"""FFmpeg-based video processing service."""
import os
import asyncio
import shutil
import uuid
import ffmpeg
from app.core.config import settings
from app.core.logs import log_event
from app.services.storage import abs_path


def check_dependencies() -> dict:
    """检测系统能否找到并运行 ffmpeg / ffprobe（按当前 PATH 解析）。
    返回 {ffmpeg:{ok,path,version,error}, ffprobe:{...}, ok:bool}。
    打包版会把包内 ffmpeg 目录放进 PATH，所以这里能反映"实际会用到的那个"。
    """
    import subprocess
    result: dict = {}
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        info = {"ok": False, "path": path, "version": None, "error": None}
        if not path:
            info["error"] = "未在 PATH 中找到 ffmpeg/ffprobe"
        else:
            try:
                out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10)
                if out.returncode == 0:
                    info["ok"] = True
                    lines = out.stdout.splitlines()
                    info["version"] = lines[0] if lines else ""
                else:
                    info["error"] = (out.stderr or out.stdout or "")[:200]
            except Exception as e:
                info["error"] = str(e)[:200]
        result[tool] = info
    result["ok"] = result["ffmpeg"]["ok"] and result["ffprobe"]["ok"]
    return result


# 归一化目标规格：对齐海外投放平台的竖屏广告标准。2026-07-20 查证 Meta 官方开发者文档
# （developers.facebook.com Reels Publishing API 指南）：竖屏最小分辨率 540×960，1080×1920
# 只是"推荐值"、不是硬性下限，720×1280 远高于最小值，技术上完全合规；且实拍素材本身多数就是
# 720×1280，改成该值可省掉放大到 1080p 的编码开销。不按输入动态推导——"最大宽×最大高"
# 会在横竖屏混传时产出不属于任何投放规格的畸形画布，"取首个输入帧率"则让产物规格随素材顺序漂移。
NORMALIZE_WIDTH = 720
NORMALIZE_HEIGHT = 1280
NORMALIZE_FPS = 30
NORMALIZE_AUDIO_RATE = 44100
NORMALIZE_AUDIO_LAYOUT = "stereo"
# mp4 视频轨时间刻度（timescale）也是归一化规格的一部分：concat demuxer 的 -c copy
# 要求各段刻度一致，不一致时第二段视频包时间戳错乱，整段画面被挤进几毫秒（有声无画，
# KNOWLEDGE #51）。15360 = 30fps × 512，是 ffmpeg 对 30fps 输入的 mp4 默认值；
# 剪映/QuickTime 系导出常见 600，必须对齐后才允许走 copy。
NORMALIZE_VIDEO_TIMESCALE = 15360
# 交付码率：最终成片和贴片素材共用。3000k@720p 是 2026-07-21 由 libx264 CRF 改过来的，
# 短剧低复杂度画面下画质够用。中间产物（Part 切片/拼接）仍用 8000k 留压缩余量。
DELIVERY_BITRATE = "3000k"


def _out_path(suffix: str = ".mp4") -> str:
    filename = f"{uuid.uuid4().hex}{suffix}"
    return os.path.join(settings.UPLOAD_DIR, "exports", filename)


def _concat_quote(p: str) -> str:
    """concat 列表文件里单引号包裹的路径，内部的 ' 要写成 '\\''，
    否则路径含撇号（如自定义导出目录 Jimmy's Videos）时列表解析断掉、拼接失败。"""
    return p.replace("'", "'\\''")


async def get_duration(video_path: str) -> float:
    """Return video duration in seconds using ffprobe."""
    abs_p = abs_path(video_path) if not os.path.isabs(video_path) else video_path
    loop = asyncio.get_running_loop()

    def _probe():
        probe = ffmpeg.probe(abs_p)
        return float(probe["format"]["duration"])

    return await loop.run_in_executor(None, _probe)


async def get_fps(video_path: str) -> float:
    """Return video frame rate in fps using ffprobe.

    Reads r_frame_rate (e.g. '30000/1001', '30/1') from the first video stream.
    Falls back to 30.0 if the stream has no frame rate info.
    """
    abs_p = abs_path(video_path) if not os.path.isabs(video_path) else video_path
    loop = asyncio.get_running_loop()

    def _probe():
        probe = ffmpeg.probe(abs_p)
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "video":
                rfr = stream.get("r_frame_rate") or stream.get("avg_frame_rate", "")
                if rfr and "/" in rfr:
                    num, den = rfr.split("/")
                    if int(den) > 0:
                        return round(int(num) / int(den), 4)
        return 30.0

    return await loop.run_in_executor(None, _probe)


async def get_video_info(video_path: str) -> dict:
    """Return video+audio specs via ffprobe:
    {"width", "height", "fps", "duration", "vcodec", "pix_fmt", "rotation",
     "video_time_base", "has_audio", "acodec", "audio_sample_rate", "audio_channels"}.

    Used before a copy-merge to detect spec mismatches — concat demuxer
    with `-c copy` requires uniform specs across inputs; feeding it mismatched
    streams corrupts playback at the splice point (frozen frame, drifting audio).
    """
    abs_p = abs_path(video_path) if not os.path.isabs(video_path) else video_path
    loop = asyncio.get_running_loop()

    def _probe():
        # show_data：让 ffprobe 带回流的 extradata（H.264 的 SPS/PPS 参数集十六进制）。
        # copy 拼接要求各段参数集逐字节一致——不同编码器产的码流缝进一个 mp4，
        # 容器头只能声明第一段的参数，VideoToolbox 从拼接点起 Cannot Decode（KNOWLEDGE #51）
        probe = ffmpeg.probe(abs_p, show_data=None)
        fmt_duration = probe.get("format", {}).get("duration")
        info = {
            "width": None, "height": None, "fps": 30.0,
            "duration": float(fmt_duration) if fmt_duration else 0.0,
            "vcodec": None, "pix_fmt": None, "rotation": 0,
            "video_time_base": None, "video_extradata": None,
            "has_audio": False, "acodec": None,
            "audio_sample_rate": None, "audio_channels": None,
            "audio_duration": None,
        }
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "video" and info["width"] is None:
                rfr = stream.get("r_frame_rate") or stream.get("avg_frame_rate", "")
                if rfr and "/" in rfr:
                    num, den = rfr.split("/")
                    if int(den) > 0:
                        info["fps"] = round(int(num) / int(den), 4)
                info["width"] = stream.get("width")
                info["height"] = stream.get("height")
                info["vcodec"] = stream.get("codec_name")
                info["pix_fmt"] = stream.get("pix_fmt")
                info["video_time_base"] = stream.get("time_base")
                info["video_extradata"] = stream.get("extradata")
                # 旋转元数据（iPhone 竖拍常见）：新版 ffprobe 在 side_data_list，旧版在 tags.rotate。
                # 带旋转的素材实际显示方向 ≠ 存储宽高，copy 拼接会导致画面横竖错乱，必须走重编码
                for sd in stream.get("side_data_list", []) or []:
                    if sd.get("rotation") not in (None, 0):
                        info["rotation"] = int(sd["rotation"])
                tag_rotate = (stream.get("tags") or {}).get("rotate")
                if not info["rotation"] and tag_rotate:
                    info["rotation"] = int(float(tag_rotate))
                # 部分素材（重新封装/录屏导出）format.duration 缺失，退回用视频流自身的 duration
                if info["duration"] <= 0 and stream.get("duration"):
                    info["duration"] = float(stream["duration"])
            elif stream.get("codec_type") == "audio" and not info["has_audio"]:
                info["has_audio"] = True
                info["acodec"] = stream.get("codec_name")
                sr = stream.get("sample_rate")
                info["audio_sample_rate"] = int(sr) if sr else None
                info["audio_channels"] = stream.get("channels")
                if stream.get("duration"):
                    info["audio_duration"] = float(stream["duration"])
        if info["duration"] <= 0:
            # 探测"成功"但拿不到有效时长——不能当 0 用（0 会让分组上限判断失效），
            # 交给调用方按探测失败处理（admake.py 的 probe 异常分支会转成 400 报错）
            raise ValueError(f"无法探测到有效时长: {abs_p}")
        return info

    return await loop.run_in_executor(None, _probe)


async def cut_video(
    source_path: str,
    start_sec: float,
    end_sec: float,
    out_path: str | None = None,
) -> str:
    """Cut using re-encode (accurate, used for export_video background)."""
    abs_src = abs_path(source_path) if not os.path.isabs(source_path) else source_path
    if out_path is None:
        out_path = _out_path()
    duration = end_sec - start_sec
    loop = asyncio.get_running_loop()

    def _run():
        (
            ffmpeg
            .input(abs_src, ss=start_sec, t=duration)
            .output(out_path, c="copy", avoid_negative_ts="make_zero")
            .overwrite_output()
            .run(quiet=True)
        )

    await loop.run_in_executor(None, _run)
    return out_path


async def cut_video_copy(
    source_path: str,
    start_sec: float,
    end_sec: float,
    out_path: str,
    normalize: bool = False,
) -> str:
    """
    Cut a segment with re-encode for frame-accurate output.

    Stream copy is NOT used because:
    1. It can only seek to keyframe boundaries (content starts at wrong position)
    2. If the source has timestamp drift (container header ≠ actual frames),
       stream copy copies the actual frames and ignores the -t limit,
       producing clips that are too long or contain wrong content.

    normalize=True 时同时归一化到 NORMALIZE_* 目标规格（720x1280@30 + AAC 44.1k 立体声）：
    广告成片链路（长素材切 Part、导出切片）必须开启，否则切出的视频保留源规格，
    与其他 Part/切片混规格 concat 时再次损坏。短剧分集裁切保持原规格，不开。
    """
    abs_src = abs_path(source_path) if not os.path.isabs(source_path) else source_path
    duration = end_sec - start_sec
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    loop = asyncio.get_running_loop()

    def _run():
        src = ffmpeg.input(abs_src, ss=start_sec, t=duration)
        if normalize:
            vf_chain = (
                f"setpts=PTS-STARTPTS,"
                f"scale={NORMALIZE_WIDTH}:{NORMALIZE_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={NORMALIZE_WIDTH}:{NORMALIZE_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={NORMALIZE_FPS},setsar=1"
            )
            af_chain = (
                f"asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates={NORMALIZE_AUDIO_RATE}:channel_layouts={NORMALIZE_AUDIO_LAYOUT}"
            )
        else:
            vf_chain, af_chain = "setpts=PTS-STARTPTS", "asetpts=PTS-STARTPTS"
        common = dict(vf=vf_chain, af=af_chain,
                      acodec="aac", audio_bitrate="128k", movflags="+faststart", bf=0,
                      video_track_timescale=NORMALIZE_VIDEO_TIMESCALE)
        try:
            # macOS 硬件编码器 — CPU 占用极低
            (src.output(out_path, vcodec="h264_videotoolbox", video_bitrate="8000k",
                        allow_sw=1, pix_fmt="yuv420p", **common)
                .overwrite_output().run(quiet=True))
        except ffmpeg.Error:
            # 降级到软件编码
            (src.output(out_path, vcodec="libx264", crf="23", preset="fast", **common)
                .overwrite_output().run(quiet=True))

    await loop.run_in_executor(None, _run)
    return out_path


async def export_final_video(
    input_paths: list[str],
    out_path: str,
    width: int = NORMALIZE_WIDTH,
    height: int = NORMALIZE_HEIGHT,
    crf: int = 20,
) -> str:
    """
    Merge all clips and fully re-encode to a single output file.

    Encoding spec (as per 1.0.13):
    - Video: H.264, CRF=20, preset=medium
    - Scale: fit within {width}x{height}, pad with black (portrait default 720x1280)
    - Audio: AAC, 128 kbps (full re-encode eliminates multi-clip AAC priming artifacts)
    - movflags=+faststart for web-friendly MP4
    - Full re-encode also normalises keyframes and timestamps across clips,
      solving seeking issues from multi-segment concat.
    """
    list_path = out_path + ".concat.txt"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    with open(list_path, "w", encoding="utf-8") as f:
        for p in input_paths:
            ap = abs_path(p) if not os.path.isabs(p) else p
            f.write(f"file '{_concat_quote(ap)}'\n")

    loop = asyncio.get_running_loop()

    def _run():
        try:
            # setpts=PTS-STARTPTS resets video PTS to 0 — prevents VideoToolbox
            # from writing an ELST edit list that offsets the start time, which
            # causes players (QuickTime etc.) to show a black first frame.
            # fps=固定帧率保证成片 CFR（Meta 要求固定帧率）；aformat 统一音频规格，
            # 防止旧项目遗留的非标 Part 混入时输出参数漂移
            vf_chain = (
                f"setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={NORMALIZE_FPS},setsar=1"
            )
            af_chain = (
                f"asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates={NORMALIZE_AUDIO_RATE}:channel_layouts={NORMALIZE_AUDIO_LAYOUT}"
            )
            src = ffmpeg.input(list_path, format="concat", safe=0)
            common = dict(vf=vf_chain, af=af_chain,
                          acodec="aac", audio_bitrate="128k",
                          movflags="+faststart", bf=0,
                          video_track_timescale=NORMALIZE_VIDEO_TIMESCALE)
            try:
                # 硬编 videotoolbox 3000k（2026-07-21 由 libx264 CRF 改）：短剧低复杂度画面下
                # 3000k@720p 画质够用，比软编快约 4.8×、CPU 减半，色彩经逐帧对齐验证与 libx264 一致。
                # 代价：固定码率不按内容自适应，高动态镜头余量偏小；文件 ~130MB，远低于平台 500MB 上限。
                (src.output(out_path, vcodec="h264_videotoolbox", video_bitrate=DELIVERY_BITRATE,
                            allow_sw=1, pix_fmt="yuv420p", **common)
                    .overwrite_output().run(quiet=True))
            except ffmpeg.Error:
                # videotoolbox 不可用时降级 CRF 软编码（体积更小、质量自适应，但慢）
                (src.output(out_path, vcodec="libx264", crf=str(crf), preset="medium",
                            pix_fmt="yuv420p", **common)
                    .overwrite_output().run(quiet=True))
        finally:
            if os.path.exists(list_path):
                os.remove(list_path)

    await loop.run_in_executor(None, _run)
    return out_path


async def normalize_to_target(src_path: str, out_path: str) -> None:
    """把一条素材重编码成归一化目标规格：720×1280@30 / H.264 yuv420p / AAC 44.1k 立体声。

    只为一件事：让它之后能走 merge_videos 的 `-c copy` 快路径。

    背景（2026-08-08 查出的体积回归）：模板配了片尾贴片时，成片最后要跟贴片拼一次。
    贴片素材是 1706×2560，不等于归一化目标，merge_videos 只好走重编码分支——
    而那个分支写死 8000k，于是整条 3000k 的正片被 8000k 重压一遍，体积翻 2.66 倍
    （同一天同一项目：带片尾的批次 397MB，不带的 149MB）。
    贴片本身只有几秒，提前压一次的代价可以忽略；正片是几百秒，重压一次代价是全部。

    码率取交付码率而不是 8000k：贴片是这条链路的终点素材，没有下游再压一次。
    """
    src = abs_path(src_path) if not os.path.isabs(src_path) else src_path
    info = await get_video_info(src)
    dur = float(info.get("duration") or 0.0)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    loop = asyncio.get_running_loop()

    def _run():
        inp = ffmpeg.input(src)
        v = (
            inp.video
            .filter("scale", NORMALIZE_WIDTH, NORMALIZE_HEIGHT,
                    force_original_aspect_ratio="decrease")
            .filter("pad", NORMALIZE_WIDTH, NORMALIZE_HEIGHT,
                    "(ow-iw)/2", "(oh-ih)/2", "black")
            .filter("fps", fps=NORMALIZE_FPS)
            .filter("setsar", 1)
        )
        if info["has_audio"]:
            # 音轨补齐/裁齐到视频长度：_at_normalize_target 要求两者相差不到 0.2 秒，
            # 差太多就不让走 copy，等于白归一化
            a = (
                inp.audio
                .filter("aformat", sample_rates=NORMALIZE_AUDIO_RATE,
                        channel_layouts=NORMALIZE_AUDIO_LAYOUT)
                .filter("apad")
                .filter("atrim", end=dur)
            )
        else:
            # 无声贴片要补一条静音轨，否则 has_audio 为假，同样过不了 copy 的门槛
            a = ffmpeg.input(
                f"anullsrc=channel_layout={NORMALIZE_AUDIO_LAYOUT}:sample_rate={NORMALIZE_AUDIO_RATE}",
                f="lavfi", t=dur,
            ).audio
        common = dict(acodec="aac", audio_bitrate="128k", pix_fmt="yuv420p",
                      movflags="+faststart", bf=0,
                      video_track_timescale=NORMALIZE_VIDEO_TIMESCALE)
        try:
            (ffmpeg.output(v, a, out_path, vcodec="h264_videotoolbox",
                           video_bitrate=DELIVERY_BITRATE, allow_sw=1, **common)
                .overwrite_output().run(quiet=True))
        except ffmpeg.Error:
            (ffmpeg.output(v, a, out_path, vcodec="libx264", crf="20",
                           preset="medium", **common)
                .overwrite_output().run(quiet=True))

    await loop.run_in_executor(None, _run)


async def at_normalize_target(path: str) -> bool:
    """外部用：这条素材是不是已经等于归一化规格（等于就不用再压一次）。"""
    return _at_normalize_target(await get_video_info(path))


def _at_normalize_target(i: dict) -> bool:
    """输入是否已经完全等于归一化目标规格（可安全走 -c copy 快速拼接）。

    比对必须覆盖视频编码/像素格式/旋转元数据：仅比分辨率帧率时，HEVC 混 H.264、
    或 iPhone 竖拍（存储横向+rotate 元数据）仍会走 copy 产出坏片。同时要求规格
    恰好等于目标值（而非"彼此一致"）——否则 copy 出的 Part 保留原始规格，与其他
    归一化 Part 混合导出时 concat 输入再次不一致。
    视频轨时间刻度同样必须等于目标值：刻度不一致的输入走 copy 拼接，第二段的
    画面帧会被 mp4 muxer 的非单调 DTS 钳制全部挤进几毫秒，成片该段有声无画
    （KNOWLEDGE #51，2026-08-28 片尾贴片实测）。
    """
    return (
        i["width"] == NORMALIZE_WIDTH and i["height"] == NORMALIZE_HEIGHT
        and abs(i["fps"] - NORMALIZE_FPS) < 0.05
        and i["video_time_base"] == f"1/{NORMALIZE_VIDEO_TIMESCALE}"
        and i["vcodec"] == "h264" and i["pix_fmt"] == "yuv420p"
        and not i["rotation"]
        and i["has_audio"] and i["acodec"] == "aac"
        and i["audio_sample_rate"] == NORMALIZE_AUDIO_RATE
        and i["audio_channels"] == 2
        # 音轨明显短于视频的素材不许走 copy：拼接后后续段声音整体前移，
        # 必须走重编码路径由 apad 补齐（探测不到音轨时长时同样保守处理）
        and i["audio_duration"] is not None
        and abs(i["audio_duration"] - i["duration"]) < 0.2
    )


def _strip_edit_list(path: str) -> None:
    """抹掉 mp4 里的 edit list（elst），让视频流真正从 0 开始。

    concat demuxer 的 `-c copy` 会给输出写一条 edit list，视频流起始时间变成 0.023 秒
    （实测 start_pts=353）。播放器按它跳过开头那一小段，第一帧就显示黑屏——运营反馈的
    「导出视频应用模板，第一帧还是黑的」就是这个。

    2026-08-11 实测确认了三件事：① 偏移是 copy 拼接这一步产生的，拼接前的两个输入
    start_pts 都是 0；② 在编码参数上加 setpts=PTS-STARTPTS 或 bf=0 完全无效——阶段①的
    产物本来就没有偏移，之前几轮修在这上面，所以怎么改都还是黑的；③ 输出侧的
    -avoid_negative_ts make_zero / -muxdelay 0 / -movflags +negative_cts_offsets /
    -start_at_zero / -fflags +genpts 等八种组合全部无效。唯一有效的是重新 remux 一遍、
    读取时用 -ignore_editlist 忽略输入的 edit list，纯封装操作，不重新编码。

    remux 失败不抛错：最坏结果是首帧黑，不值得让整条导出失败。
    """
    tmp = path + ".noelst.mp4"
    try:
        (
            ffmpeg
            .input(path, ignore_editlist=1)
            .output(tmp, c="copy", movflags="+faststart")
            .overwrite_output()
            .run(quiet=True)
        )
        os.replace(tmp, path)
    except (ffmpeg.Error, OSError) as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        log_event("video.strip_edit_list.failed", path=os.path.basename(path),
                  error=str(e)[:200])


async def merge_videos(input_paths: list[str], out_path: str | None = None,
                       limits: list[float | None] | None = None) -> str:
    """Merge clips into one file.

    Uses fast stream-copy (`-c copy`) only when every input is already exactly at the
    normalization target (720x1280@30 H.264 yuv420p + AAC 44.1k stereo, no rotation).
    Everything else re-encodes to that target, so every merged Part comes out with
    identical specs — concat demuxer copy on mismatched streams corrupts playback at
    the splice point, and mixed-spec Parts would break the export concat later.
    """
    if out_path is None:
        out_path = _out_path()

    abs_paths = [abs_path(p) if not os.path.isabs(p) else p for p in input_paths]
    infos = [await get_video_info(p) for p in abs_paths]
    limits = list(limits) if limits else [None] * len(abs_paths)

    # limits[i] 是只取第 i 个输入的前多少秒（片尾板检测出来的截断点）。
    # 有截断就必须重编码：concat demuxer 的 -c copy 只能在关键帧上切，
    # 归一化后关键帧间隔约 2 秒，而片尾板本身才 2 秒出头——按关键帧对齐会
    # 要么留半截片尾板、要么吃掉正片最后一两秒。
    trimming = any(l is not None and l < i["duration"] - 0.05
                   for l, i in zip(limits, infos))
    uniform = all(_at_normalize_target(i) for i in infos) and not trimming

    # copy 拼接还要求所有输入的视频参数集（SPS/PPS）逐字节一致：规格全对但出自
    # 不同编码器的两段码流缝进一个 mp4，容器头只声明第一段的参数集，ffmpeg 软解
    # 能读码流内参数所以看着正常，VideoToolbox（QuickTime/浏览器硬解）按容器头
    # 初始化，从拼接点起 Cannot Decode——成片后半段有声无画（KNOWLEDGE #51 第二层）。
    # 探测不到参数集（None）按不一致处理：宁可多编一次，不出解不了的片。
    if uniform:
        extradatas = {i["video_extradata"] for i in infos}
        if len(extradatas) > 1 or None in extradatas:
            uniform = False
            log_event("video.merge_reencode", reason="extradata_mismatch",
                      out_path=out_path,
                      extradata_sizes=[len(i["video_extradata"] or "") for i in infos])

    loop = asyncio.get_running_loop()

    if uniform:
        list_path = out_path + ".txt"
        with open(list_path, "w") as f:
            for p in abs_paths:
                f.write(f"file '{_concat_quote(p)}'\n")

        def _run_copy():
            try:
                (
                    ffmpeg
                    .input(list_path, format="concat", safe=0)
                    .output(out_path, c="copy")
                    .overwrite_output()
                    .run(quiet=True)
                )
            finally:
                # 失败也要清列表文件——自定义导出目录时它会留在用户文件夹里
                try:
                    os.remove(list_path)
                except OSError:
                    pass

        await loop.run_in_executor(None, _run_copy)
        await loop.run_in_executor(None, _strip_edit_list, out_path)
        return out_path

    log_event(
        "video.merge_reencode", reason="not_at_normalize_target", out_path=out_path,
        widths=[i["width"] for i in infos], heights=[i["height"] for i in infos],
        fps=[i["fps"] for i in infos], vcodecs=[i["vcodec"] for i in infos],
        pix_fmts=[i["pix_fmt"] for i in infos], rotations=[i["rotation"] for i in infos],
        has_audio=[i["has_audio"] for i in infos], acodecs=[i["acodec"] for i in infos],
        audio_sample_rates=[i["audio_sample_rate"] for i in infos],
        audio_channels=[i["audio_channels"] for i in infos],
    )

    width, height, fps = NORMALIZE_WIDTH, NORMALIZE_HEIGHT, NORMALIZE_FPS
    sample_rate, layout = NORMALIZE_AUDIO_RATE, NORMALIZE_AUDIO_LAYOUT

    # 同一文件出现多次（如模板前后贴片用同一个素材）时，ffmpeg-python 会把相同输入
    # 合并成一个图节点，构图报 "split filter required" 直接崩。给重复项各复制一份
    # 唯一临时文件绕开（copy 快速路径不受影响，只有重编码分支需要）。
    dup_copies: list[str] = []
    seen: dict[str, int] = {}
    uniq_paths: list[str] = []
    for p in abs_paths:
        if p in seen:
            seen[p] += 1
            root, ext2 = os.path.splitext(p)
            # uuid 后缀：源文件在共享目录（如模板素材），并发的两次导出各自建/删同名副本会互相拆台
            cp = f"{root}.dup{uuid.uuid4().hex[:8]}{ext2}"
            shutil.copyfile(p, cp)
            dup_copies.append(cp)
            uniq_paths.append(cp)
        else:
            seen[p] = 0
            uniq_paths.append(p)

    def _run_reencode():
        streams = []
        for p, info, lim in zip(uniq_paths, infos, limits):
            seg_dur = min(lim, info["duration"]) if lim is not None else info["duration"]
            inp = ffmpeg.input(p, **({"t": round(seg_dur, 3)} if lim is not None else {}))
            v = (
                inp.video
                .filter("scale", width, height, force_original_aspect_ratio="decrease")
                .filter("pad", width, height, "(ow-iw)/2", "(oh-ih)/2", "black")
                .filter("fps", fps=fps)
                .filter("setsar", 1)
            )
            if info["has_audio"]:
                # apad+atrim 把音轨补齐/裁齐到这一段该有的长度：concat filter 不会自动
                # 对齐段内音视频长度差（ffmpeg 官方文档明确建议 pad 短流），音轨偏短的
                # 素材会让后续所有段的声音提前，误差逐段累积。
                # 长度必须取截断后的值——写死原始时长会把音轨补回全长，
                # concat 的输出长度跟最长的流走，截断就白做了。
                a = (
                    inp.audio
                    .filter("aformat", sample_rates=sample_rate, channel_layouts=layout)
                    .filter("apad")
                    .filter("atrim", end=seg_dur)
                )
            else:
                # 无音轨的素材（纯背景/B-roll）合成静音轨，保证 concat 每段的 a=1 都能对上
                a = ffmpeg.input(
                    f"anullsrc=channel_layout={layout}:sample_rate={sample_rate}",
                    f="lavfi", t=seg_dur,
                ).audio
            streams += [v, a]
        joined = ffmpeg.concat(*streams, v=1, a=1).node
        common = dict(acodec="aac", audio_bitrate="128k", pix_fmt="yuv420p",
                      movflags="+faststart", bf=0,
                      video_track_timescale=NORMALIZE_VIDEO_TIMESCALE)
        try:
            (
                ffmpeg.output(joined[0], joined[1], out_path,
                              vcodec="h264_videotoolbox", video_bitrate="8000k",
                              allow_sw=1, **common)
                .overwrite_output().run(quiet=True)
            )
        except ffmpeg.Error as e:
            # 硬件编码器（videotoolbox）并发 session 数超限时会在这里报错兜底到软编，
            # 排查 build-parts 并发度是否顶到硬件上限时看这条日志有没有出现即可。
            log_event("video.merge_reencode.hw_fallback", out_path=out_path, error=str(e)[:200])
            (
                ffmpeg.output(joined[0], joined[1], out_path,
                              vcodec="libx264", crf="20", preset="medium", **common)
                .overwrite_output().run(quiet=True)
            )

    try:
        await loop.run_in_executor(None, _run_reencode)
    finally:
        for cp in dup_copies:
            try:
                os.remove(cp)
            except OSError:
                pass
    return out_path


async def compress_for_ai(video_path: str, out_path: str) -> tuple[str, int]:
    """Compress video for AI analysis upload: H.264, 1280x720 or 720x1280, 15fps, CRF=32, preset=ultrafast.

    Uses CRF (constant quality) instead of fixed bitrate so low-bitrate sources are never
    inflated. Returns (out_path, skip_bytes) where skip_bytes > 0 means compression was
    skipped because ffmpeg produced a larger file — skip_bytes is the inflated size in that
    case and out_path now contains a copy of the original.

    速度优先（2026-07-21）：产物只供 Gemini 分析、用完即删，画质不重要。preset 从 medium
    降到 ultrafast、帧率 30→15（Gemini 按 ~1fps 抽帧，15 帧足够），单集压缩约 66s→7s（实测 9 倍），
    CPU 满载窗口大幅缩短，文件仍 ~45MB（与原 medium 相当、上传不变；crf32 补偿 ultrafast 的码率膨胀）。
    分辨率保持 720p 不动——Gemini 打点靠内容理解，720p 零风险，无需为省 token 降到 480p（价格次要）。"""
    abs_src = abs_path(video_path) if not os.path.isabs(video_path) else video_path
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    loop = asyncio.get_running_loop()
    orig_size = os.path.getsize(abs_src)

    def _run():
        probe = ffmpeg.probe(abs_src)
        w, h = 1280, 720
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "video":
                if (stream.get("height", 0) or 0) > (stream.get("width", 0) or 0):
                    w, h = 720, 1280  # portrait (vertical short drama)
                break
        (
            ffmpeg
            .input(abs_src)
            .output(
                out_path,
                vcodec="libx264",
                crf=32,
                r=15,
                vf=f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
                acodec="aac",
                audio_bitrate="128k",
                preset="ultrafast",
                movflags="+faststart",
            )
            .overwrite_output()
            .run(quiet=True)
        )
        encoded_size = os.path.getsize(out_path)
        if encoded_size >= orig_size:
            # Compressed file is larger — skip compression and use original
            shutil.copy2(abs_src, out_path)
            return encoded_size  # return inflated size so caller can log it
        return 0  # 0 = compression was beneficial

    skip_bytes = await loop.run_in_executor(None, _run)
    return out_path, skip_bytes


async def extract_thumbnail(video_path: str, time_sec: float = 1.0) -> str:
    """Extract a single frame as JPEG thumbnail."""
    abs_src = abs_path(video_path) if not os.path.isabs(video_path) else video_path
    out_path = os.path.join(settings.UPLOAD_DIR, "DramaCreate", "images", f"{uuid.uuid4().hex}.jpg")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    loop = asyncio.get_running_loop()

    def _run():
        (
            ffmpeg
            .input(abs_src, ss=time_sec)
            .output(out_path, vframes=1, format="image2")
            .overwrite_output()
            .run(quiet=True)
        )

    await loop.run_in_executor(None, _run)
    return out_path

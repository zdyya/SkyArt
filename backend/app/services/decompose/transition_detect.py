"""源视频片头/片尾转场段检测。

短剧每集的结尾几乎都接一段"片尾板"：先来一次转场特效，然后是定格画面、缓慢缩放
或者带粒子的静态画面。这一段混进 Part 会污染 AI 分析和成片。

## 判定依据是"画面剧变"，不是"连续静止"

初版按"从尾部往前找连续静止帧"来找，实测 919 条源视频一条都没检出。原因有两层：
一是真定格常常只有 0.5 秒，被"连续 1 秒才算"的门槛当成单帧抖动丢掉；二更根本——
**片尾板经常根本不是静止的**（实测样本里有带粒子飘动的、有缓慢位移的），
靠"静止"这个特征从原理上就抓不到它们。

实测四个样本用了三种不同的转场手法：

- 纯白闪（整帧亮度冲到 250）
- 暗底上的金色爆闪（亮度只有 147，绝对亮度门槛抓不到）
- 径向冲击变焦模糊（亮度几乎不变，亮度法完全失效）

三者的共同点是**相邻帧之间出现一次远超正常剪辑的巨变**：闪光处的帧间残差是窗口
中位数的 13~24 倍，而正常的镜头硬切才 3~5 倍。所以拿跳变当锚点，跳变之后到结尾的
全部内容一律算片尾板——不管它后面是定格、Zoom 还是粒子动效。

锚点取的是**"剩余时长合格的跳变里最强的那个"**，不是全局最强的那个。剧情结尾的爆闪
可以比片尾转场更强（实测有 19.6 倍 vs 11.9 倍的例子），先取最强就会锚到剧情上，
算出的片尾板长度超上限后整段放弃，那一集就一点没剪。也不能改成取最靠后的合格跳变
——片尾板内部的粒子飘动同样会产生合格跳变，会少剪一截。

## 切点落在闪光之后，不是之前

最大的单帧跳变正是"白→画面"那次硬回切：跳变前的最后一帧还是闪光，跳变后的第一帧
就是片尾板的头一帧。所以切点取跳变后的第一帧——**闪光连同它之前的内容一律保留**。

曾经把切点往回推到闪光起点，理由是"起光那半秒也算转场"。这是错的：闪光是剧情的
重音，闪光之前的一秒多是全集最精彩的段落，推到起点等于把高潮剪掉。要剪的只是
闪光之后那块定格/缓放的片尾板。

## 护栏

跳变强度要够（`_SPIKE_K`），且跳变之后的剩余时长要落在合理区间（`_MIN_TAIL`~`_MAX_TAIL`）。
剧情内部的硬切也会产生大残差，靠"剩余时长"这一条把它们排除掉——真片尾板就那么长。
不满足就返回 0，宁可漏裁不错裁。
"""
import json
import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageChops, ImageStat

_WIN = 12.0       # 检测窗口：首尾各看这么多秒
_STEP = 0.1       # 抽帧步长，够分辨闪光的起落
_FRAME_W = 128    # 分析分辨率（宽），灰度，够看清整帧变化又算得快
_SPIKE_K = 2.2    # 跳变门槛：帧间残差要达到窗口中位数的这个倍数
_MIN_TAIL = 0.4   # 转场段时长的合理区间，越界视为抓错
_MAX_TAIL = 3.5
_MIN_FRAMES = 8   # 帧数不足就不判


def _grab_frames(path: str, ss: float, dur: float, out_dir: str) -> list[Image.Image]:
    ff = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ff, "-y", "-loglevel", "error", "-ss", f"{ss:.3f}", "-i", path,
        "-vf", f"fps={1 / _STEP},scale={_FRAME_W}:-2,format=gray",
        "-frames:v", str(int(dur / _STEP) + 1),
        os.path.join(out_dir, "f%04d.png"),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return [Image.open(os.path.join(out_dir, n))
            for n in sorted(os.listdir(out_dir)) if n.endswith(".png")]


def _series(path: str, ss: float, dur: float) -> tuple[list[float], list[float]]:
    """返回窗口内的 (帧间残差序列, 逐帧平均亮度序列)。残差比亮度少一项。"""
    with tempfile.TemporaryDirectory() as tmp:
        frames = _grab_frames(path, ss, dur, tmp)
        if len(frames) < _MIN_FRAMES:
            return [], []
        res = [ImageStat.Stat(ImageChops.difference(frames[i], frames[i + 1])).mean[0]
               for i in range(len(frames) - 1)]
        lum = [ImageStat.Stat(f).mean[0] for f in frames]
    return res, lum


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


def _detect_one(path: str, ss: float, win: float, from_tail: bool) -> float:
    """在一个窗口里找转场段，返回它的时长（秒）；找不到返回 0。"""
    res, lum = _series(path, ss, win)
    if not res:
        return 0.0
    med = _median(res) or 0.01

    if from_tail:
        # 先按"强度达标 + 跳变之后只剩 _MIN_TAIL~_MAX_TAIL 秒"筛出候选，再在候选里取最强的
        # 那个。顺序很要紧：原来是反的——先取全局最强、再检查剩余时长，一旦最强的那个跳变
        # 不是片尾转场，整段就返回 0 放弃。
        #
        # 2026-08-11 运营反馈某一集的定格结尾一点没剪掉。查出来那集 48.82 秒处有个剧情
        # 爆闪（亮度 27→122，残差 19.6 倍中位数），盖过了 58.02 秒处真正的片尾转场
        # （11.9 倍）。先取最强就取到爆闪上，算出片尾板 11.8 秒、超过 3.5 秒上限后放弃。
        # 同一批另外四集之所以没事，只是它们的最强跳变恰好就是片尾转场。
        #
        # 也不能改成"取最靠后的合格跳变"——片尾板内部的粒子飘动、缓放同样会产生合格跳变，
        # 实测那四集会各自少剪 0.1~0.4 秒，片尾板留下一截。
        #
        # res[i] 是第 i 帧与第 i+1 帧之间的差。跳变在 i，意味着第 i 帧还是闪光、
        # 第 i+1 帧已经是片尾板的头一帧——切点取 i+1，闪光整个留在 Part 里。
        cands = [i for i in range(len(res))
                 if res[i] >= med * _SPIKE_K
                 and _MIN_TAIL <= win - (i + 1) * _STEP <= _MAX_TAIL]
        if not cands:
            return 0.0
        best = max(cands, key=lambda i: res[i])
        return win - (best + 1) * _STEP

    # 片头：窗口开始到跳变之前都是片头板。片头板后面接的是剧情，所以切点取跳变本身
    # （闪光属于片头板，剧情从跳变后的第一帧起）。这条路径 detect_transitions 里恒不启用，
    # 判据对片头不成立，保留是为了 probe_detail 复核时能看到中间量。
    peak_i = max(range(len(res)), key=lambda i: res[i])
    if res[peak_i] < med * _SPIKE_K:
        return 0.0
    seg = (peak_i + 1) * _STEP
    return seg if _MIN_TAIL <= seg <= _MAX_TAIL else 0.0


def detect_transitions(path: str, duration: float) -> tuple[float, float]:
    """返回 (片头转场段秒数, 片尾转场段秒数)。检不到或不确定一律返回 0。"""
    if duration <= 2 * _WIN:
        return 0.0, 0.0
    try:
        tail = _detect_one(path, duration - _WIN, _WIN, from_tail=True)
    except Exception:
        return 0.0, 0.0
    # 片头恒返回 0：这套判据只对片尾成立。片尾靠"跳变之后到结尾只剩 0.4~3.5 秒"
    # 这条硬约束把剧情内部的镜头切换排除掉；片头没有对等约束——跳变后面紧接着就是
    # 剧情，长度不受限。实测一条样片开头 2 秒是正常剧情（跑车、人物入画），
    # 一次普通镜头切换就被判成片头板，切下去会吃掉开场。
    # 要做片头得换特征（识别标题卡/台标），目前没有这个需求。
    return 0.0, round(tail, 2)


def probe_detail(path: str, duration: float) -> dict:
    """人工复核用：把判定过程的中间量一起返回，不参与线上逻辑。"""
    out = {"duration": round(duration, 2)}
    for label, ss, from_tail in (("head", 0.0, False), ("tail", duration - _WIN, True)):
        res, lum = _series(path, max(0.0, ss), _WIN)
        if not res:
            out[label] = {"seg": 0.0, "why": "帧数不足"}
            continue
        med = _median(res) or 0.01
        peak_i = max(range(len(res)), key=lambda i: res[i])
        # 片尾按线上逻辑取"合格候选里最强的那个"，同时把全局最大那个也报出来——两者不是
        # 同一个跳变时，正说明有更强的剧情爆闪盖在后面（2026-08-11 的漏剪就是这么查出来的）。
        chosen_i = peak_i
        if from_tail:
            cands = [i for i in range(len(res))
                     if res[i] >= med * _SPIKE_K
                     and _MIN_TAIL <= _WIN - (i + 1) * _STEP <= _MAX_TAIL]
            chosen_i = max(cands, key=lambda i: res[i]) if cands else None
        d = {
            "peak_sec": round(max(0.0, ss) + peak_i * _STEP, 2),
            "peak_residual": round(res[peak_i], 1),
            "median_residual": round(med, 1),
            "ratio": round(res[peak_i] / med, 1),
            "baseline_lum": round(_median(lum), 1),
            "peak_lum": round(lum[peak_i], 1),
        }
        if chosen_i is None:
            d.update({"seg": 0.0, "accepted": False, "why": "没有跳变同时满足强度与剩余时长"})
            out[label] = d
            continue
        if chosen_i != peak_i:
            d["chosen_sec"] = round(max(0.0, ss) + chosen_i * _STEP, 2)
            d["chosen_ratio"] = round(res[chosen_i] / med, 1)
            d["why"] = "全局最大跳变的剩余时长越界，改取合格候选里最强的那个"
        if from_tail:
            d["cut_sec"] = round(max(0.0, ss) + (chosen_i + 1) * _STEP, 2)
            d["seg"] = round(_WIN - (chosen_i + 1) * _STEP, 2)
        else:
            d["seg"] = round((chosen_i + 1) * _STEP, 2)
        d["accepted"] = bool(res[chosen_i] >= med * _SPIKE_K and _MIN_TAIL <= d["seg"] <= _MAX_TAIL)
        out[label] = d
    return out


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        o = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", p],
            capture_output=True, text=True).stdout
        dur = float(json.loads(o)["format"]["duration"])
        print(os.path.basename(p), detect_transitions(p, dur))

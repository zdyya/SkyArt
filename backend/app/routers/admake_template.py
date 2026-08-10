"""成片模板 CRUD 路由。挂在 /admake 前缀下，与 admake.py 同域。
模板本体存 admake_templates（config 为 JSON 字符串）；素材文件存
{UPLOAD_DIR}/AdMake/Templates/template_{id}/，config 里按文件名引用（素材上传见增量 3）。"""
import asyncio
import glob
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile

from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.admake import AdmakeTemplate
from app.schemas.admake import (
    AdmakeTemplateCreate, AdmakeTemplateUpdate, AdmakeTemplateOut,
)
from app.core.deps import get_current_user
from app.core.config import settings
from app.services import video
from app.core.logs import log_failure
from app.core.logs import log_event
from app.services.video import merge_videos

router = APIRouter(prefix="/admake", tags=["AdMake"])


def _template_dir(template_id: int) -> str:
    """模板素材目录（前后贴片 / PNG / 动态 MOV 存这里）。

    返回绝对路径。这里的路径会传给 video.get_video_info 等函数，那些函数遇到
    相对路径会再拼一次 UPLOAD_DIR，拼成 uploads/uploads/... 后 ffprobe 直接失败。
    打包版的 UPLOAD_DIR 是绝对路径碰不到，开发态默认的 ./uploads 会踩中。"""
    return os.path.abspath(
        os.path.join(settings.UPLOAD_DIR, "AdMake", "Templates", f"template_{template_id}"))


# 素材类型：clip=前后贴片（不要求透明）；png=静态贴纸（必须透明）；mov=动态特效（必须透明视频）
_ASSET_KINDS = {"clip", "png", "mov"}

# 素材大小上限：整文件读进内存/无限写盘都会拖垮后端，超限当场拒收并给明确文案。
# clip 是普通压缩视频给宽些；mov 是 qtrle 无损、正常几十 MB 内；png 静态图。
# 另一份在 electron-app/renderer/tools/ad-make/template-editor.html 的前后贴片提示文案。
_ASSET_MAX_MB = {"clip": 200, "png": 20, "mov": 100}
_PKG_MAX_MB = 500          # .skytpl 分享包上限
_UPLOAD_CHUNK = 1024 * 1024

# 前后贴片时长上限。贴片会整段接在每条成片的头尾，长度直接加进成片总时长。
# 2026-08-10：一条 188 秒的正片被当片尾存进模板，3 分钟目标的四条成片全导成 6 分 12 秒。
# 片头/片尾实际都是几秒的引导或结尾板，60 秒足够宽松，只挡明显选错文件的情况。
_CLIP_MAX_SEC = 60


async def _save_upload_capped(file: UploadFile, dest: str, max_mb: int, label: str) -> None:
    """分块把上传写到磁盘，边写边计数；超限删残件报 400（不整文件进内存）。"""
    limit = max_mb * 1024 * 1024
    written = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                f.close()
                os.remove(dest)
                raise HTTPException(400, f"{label}超过 {max_mb}MB 上限，请压缩后再导入")
            f.write(chunk)


def _png_has_alpha(path: str) -> bool:
    """PNG 是否真有透明：ffprobe 的 pal8 判不出 tRNS（不透明的调色板图也报 pal8），
    用 Pillow 看真实模式。"""
    try:
        img = Image.open(path)
        return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    except Exception:
        return False
# 带透明通道的 pix_fmt：PNG 调色板 pal8 靠 tRNS 携带透明；其余按名字里的 alpha 分量判断
_ALPHA_PIX_FMT_HINTS = ("argb", "rgba", "abgr", "bgra", "yuva", "ya8", "ya16", "gbrap")


def _has_alpha(pix_fmt: str | None) -> bool:
    if not pix_fmt:
        return False
    pf = pix_fmt.lower()
    # pal8 不算：调色板格式有没有透明取决于 tRNS 块，ffprobe 看不出——PNG 用 _png_has_alpha 真检测
    return any(h in pf for h in _ALPHA_PIX_FMT_HINTS)


def _probe_asset(path: str) -> dict:
    """轻量 ffprobe：取首个视频/图像流的 codec、pix_fmt、宽高，以及可选时长。
    不复用 get_video_info——它对无 duration 的 PNG 会抛错（KNOWLEDGE #3）。"""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt,width,height",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ValueError(f"文件无法解析（ffprobe 失败）：{proc.stderr[:150]}")
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (ValueError, TypeError):
        duration = 0.0
    return {
        "codec": stream.get("codec_name"),
        "pix_fmt": stream.get("pix_fmt"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration": round(duration, 2),
        "has_alpha": _has_alpha(stream.get("pix_fmt")),
    }


def _alpha_bbox(mov_path: str, work_dir: str) -> tuple[int, int, int, int] | None:
    """抽样几帧，用 Pillow 算非透明内容的并集边界框（覆盖会动的效果）。返回 (x,y,w,h)。
    cropdetect 对 alpha 不可靠（算出负宽），故用 getbbox。"""
    frames_dir = os.path.join(work_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    ff = shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run(
        [ff, "-y", "-loglevel", "error", "-i", mov_path, "-t", "8", "-vf", "fps=10",
         "-pix_fmt", "rgba", os.path.join(frames_dir, "f_%03d.png")],
        capture_output=True, text=True,
    )
    boxes = []
    for f in sorted(glob.glob(os.path.join(frames_dir, "*.png"))):
        try:
            bb = Image.open(f).convert("RGBA").getchannel("A").getbbox()
        except Exception:
            bb = None
        if bb:
            boxes.append(bb)
    if not boxes:
        return None
    l = min(b[0] for b in boxes)
    t = min(b[1] for b in boxes)
    r = max(b[2] for b in boxes)
    btm = max(b[3] for b in boxes)
    return (l, t, r - l, btm - t)


def _png_all_transparent(path: str) -> bool:
    """有透明通道但每个像素都全透明 = 空素材：进画布看不见，运营会以为软件坏了。"""
    try:
        with Image.open(path) as im:
            return im.convert("RGBA").getchannel("A").getextrema()[1] == 0
    except Exception:
        return False


def _mov_all_transparent(path: str) -> bool:
    """每秒抽一帧量 alpha 均值，全部为 0 = 整段完全透明（多为剪映导出时素材不在导出范围）。"""
    ff = shutil.which("ffmpeg") or "ffmpeg"
    r = subprocess.run(
        [ff, "-t", "12", "-i", path,
         "-vf", "fps=1,alphaextract,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    vals = re.findall(r"YAVG=([0-9.]+)", (r.stderr or "") + (r.stdout or ""))
    return bool(vals) and all(float(v) == 0 for v in vals)


_DYN_MAX_SEC = 10  # 动态贴纸时长上限：短循环素材，超长部分导出时也用不上（循环/截断）


def _autocrop_dyn(abs_path: str, out_dir: str, meta: dict) -> None:
    """剪映导出的动效是整块画布、元素烧在某处、四周透明。上传时一次性预处理：
    ① 裁掉透明边只留元素（能当小贴纸摆位置）；② 超过成片画布(720×1280)的等比压回画布内——
    qtrle 每次导出都要全程逐帧解码，多余分辨率纯烧 CPU；③ 超过 10 秒截取前 10 秒
    （动态贴纸按短循环用，超长部分导出时也到不了画面）。原地替换文件并更新 meta。"""
    fw, fh = meta.get("width") or 0, meta.get("height") or 0
    if not (fw and fh):
        return
    dur = float(meta.get("duration") or 0)
    cap_dur = dur > _DYN_MAX_SEC
    work = os.path.join(out_dir, f".crop_{uuid.uuid4().hex[:8]}")
    os.makedirs(work, exist_ok=True)
    try:
        vf = []
        w, h = fw, fh
        bbox = _alpha_bbox(abs_path, work)
        if bbox:
            x, y, bw, bh = bbox
            # 只有确实有透明边（内容明显小于画布）才裁；四周留 4px 余量防剪掉软边
            if bw >= 8 and bh >= 8 and (bw < fw * 0.95 or bh < fh * 0.95):
                m = 4
                x2 = max(0, x - m)
                y2 = max(0, y - m)
                r = min(fw, x + bw + m)
                b = min(fh, y + bh + m)
                x, y, w, h = x2, y2, r - x2, b - y2
                w -= w % 2  # 尺寸取偶数（编码器友好），向内收 ≤1px（已有 4px 余量兜底）
                h -= h % 2
                vf.append(f"crop={w}:{h}:{x}:{y}")
        # 分辨率封顶：动效按原始像素上 720×1280 画面，超出部分画面装不下、解码却全程要付
        if w > 720 or h > 1280:
            k = min(720 / w, 1280 / h)
            tw = max(2, int(w * k)) // 2 * 2
            th = max(2, int(h * k)) // 2 * 2
            vf.append(f"scale={tw}:{th}:flags=lanczos")
            w, h = tw, th
        if not vf and not cap_dur:
            return
        ff = shutil.which("ffmpeg") or "ffmpeg"
        cropped = os.path.splitext(abs_path)[0] + "_crop.mov"
        cmd = [ff, "-y", "-loglevel", "error", "-i", abs_path]
        if cap_dur:
            cmd += ["-t", str(_DYN_MAX_SEC)]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-c:v", "qtrle", cropped]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(cropped):
            os.replace(cropped, abs_path)
            meta["width"], meta["height"] = w, h
            if cap_dur:
                meta["duration"] = float(_DYN_MAX_SEC)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _make_dyn_preview(mov_path: str, out_dir: str, base: str, dur: float) -> tuple[str | None, str | None]:
    """动效素材浏览器放不了 qtrle，转一份 APNG(动画+透明)供编辑页预览——Chrome 原生
    `<img>` 直接放(已在真实 Chrome 验证透明生效)。转不成退回透明静帧。用 .png 扩展名让
    <img> 能识别。返回 (文件名, 'image'|None)。"""
    ff = shutil.which("ffmpeg") or "ffmpeg"
    cap = min(dur, 6) if dur and dur > 0 else 4
    apng = f"{base}.preview.png"
    ap = os.path.join(out_dir, apng)
    r = subprocess.run(
        [ff, "-y", "-loglevel", "error", "-i", mov_path, "-t", str(cap),
         "-vf", "fps=12,scale=min(360\\,iw):-2:flags=lanczos",
         "-c:v", "apng", "-f", "apng", "-pix_fmt", "rgba", "-plays", "0", ap],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and os.path.isfile(ap) and os.path.getsize(ap) > 0:
        return apng, "image"
    # 兜底：代表帧透明静图
    png = f"{base}.poster.png"
    pp = os.path.join(out_dir, png)
    ss = round((dur or 1) * 0.3, 2)
    r2 = subprocess.run(
        [ff, "-y", "-loglevel", "error", "-ss", str(ss), "-i", mov_path,
         "-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgba", pp],
        capture_output=True, text=True,
    )
    if r2.returncode == 0 and os.path.isfile(pp):
        return png, "image"
    return None, None


def _validate_asset(kind: str, meta: dict, path: str | None = None) -> None:
    """按素材类型校验；不合格抛 400 明确文案（对应交互里"导入即校验、当场拒收"）。"""
    if not meta.get("codec") or not meta.get("width"):
        raise HTTPException(400, "文件无法识别为有效的视频/图片，请重新导出后再导入")
    if kind == "png":
        if meta["codec"] != "png":
            raise HTTPException(400, "静态贴纸请上传 PNG 图片")
        # ffprobe 判不出调色板 PNG 的 tRNS（不透明的 pal8 也会误报有透明），用 Pillow 真检测
        if not (path and _png_has_alpha(path)):
            raise HTTPException(400, "这张 PNG 没有透明背景，请用带透明通道的 PNG（否则会盖成不透明方块）")
        if _png_all_transparent(path):
            raise HTTPException(400, "这张 PNG 内容全空（每个像素都是全透明），请检查导出的图层")
    elif kind == "mov":
        if not meta["has_alpha"]:
            raise HTTPException(400, "动态贴纸素材没有透明通道，请在剪映重新导出：格式选 MOV、编码选 RLE")
        if path and _mov_all_transparent(path):
            raise HTTPException(400, "动态贴纸内容全空（全透明），请检查剪映导出时贴纸/特效是否在导出范围内")
    else:
        # clip（前后贴片）不要求透明，只卡时长——超过上限基本是把整条正片当贴片选了
        clip_dur = float(meta.get("duration") or 0.0)
        if clip_dur > _CLIP_MAX_SEC:
            raise HTTPException(
                400,
                f"这条贴片有 {clip_dur:.0f} 秒，超过上限 {_CLIP_MAX_SEC} 秒。"
                f"前后贴片会整段接在每条成片的头尾，请确认没有把整条正片选成片头/片尾。",
            )


def _to_out(t: AdmakeTemplate) -> AdmakeTemplateOut:
    """把 DB 行转成响应模型：config 由 JSON 字符串解析成 dict。脏数据兜底成 {}。"""
    try:
        config = json.loads(t.config) if t.config else {}
    except (ValueError, TypeError):
        config = {}
    return AdmakeTemplateOut(
        id=t.id, name=t.name, config=config, is_default=bool(t.is_default),
        thumb_path=t.thumb_path, created_at=t.created_at, updated_at=t.updated_at,
    )


async def _get_owned_template(tid: int, user: User, db: AsyncSession) -> AdmakeTemplate:
    res = await db.execute(
        select(AdmakeTemplate).where(
            AdmakeTemplate.id == tid, AdmakeTemplate.user_id == user.id
        )
    )
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "模板不存在或无权限")
    return t


@router.get("/templates", response_model=list[AdmakeTemplateOut])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(AdmakeTemplate)
        .where(AdmakeTemplate.user_id == user.id)
        .order_by(AdmakeTemplate.updated_at.desc())
    )
    return [_to_out(t) for t in res.scalars().all()]


@router.post("/templates", response_model=AdmakeTemplateOut, status_code=201)
async def create_template(
    body: AdmakeTemplateCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "模板名不能为空")
    t = AdmakeTemplate(user_id=user.id, name=name[:100], config=json.dumps(body.config, ensure_ascii=False))
    db.add(t)
    await db.flush()
    return _to_out(t)


@router.get("/templates/{tid}", response_model=AdmakeTemplateOut)
async def get_template(
    tid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _to_out(await _get_owned_template(tid, user, db))


@router.put("/templates/{tid}", response_model=AdmakeTemplateOut)
async def update_template(
    tid: int,
    body: AdmakeTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    t = await _get_owned_template(tid, user, db)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "模板名不能为空")
        t.name = name[:100]
    if body.config is not None:
        t.config = json.dumps(body.config, ensure_ascii=False)
    await db.flush()
    return _to_out(t)


@router.delete("/templates/{tid}", status_code=204)
async def delete_template(
    tid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    t = await _get_owned_template(tid, user, db)
    asset_dir = _template_dir(t.id)
    await db.delete(t)
    await db.flush()
    # 素材目录随模板物理删除；缺失/删除失败不阻断（DB 行已删是权威结果）
    if os.path.isdir(asset_dir):
        shutil.rmtree(asset_dir, ignore_errors=True)


@router.post("/templates/{tid}/default", response_model=AdmakeTemplateOut)
async def set_default_template(
    tid: int,
    on: bool = True,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把这个模板设成（或取消）当前用户的默认模板。

    默认模板只是"新建项目和导出面板的预填值"，不是强制——用户在下拉里改成
    「不使用任何模板」照样生效。所以这里只动标记，不碰任何已存在的项目配置。

    同一用户同时只能有一个默认：先把这个人所有模板的标记清掉，再点亮这一个。
    清全部而不是只清旧的那个，是为了顺手修好历史上可能出现的多标记脏数据。
    """
    t = await _get_owned_template(tid, user, db)
    await db.execute(
        update(AdmakeTemplate)
        .where(AdmakeTemplate.user_id == user.id)
        .values(is_default=False)
    )
    if on:
        t.is_default = True
    await db.commit()
    await db.refresh(t)
    return _to_out(t)


@router.post("/templates/{tid}/duplicate", response_model=AdmakeTemplateOut, status_code=201)
async def duplicate_template(
    tid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    src = await _get_owned_template(tid, user, db)
    copy = AdmakeTemplate(
        user_id=user.id, name=f"{src.name}_副本"[:100], config=src.config,
    )
    db.add(copy)
    await db.flush()
    # 连素材一起复制，否则副本引用的文件不存在（增量 3 起素材才落地，此处向前兼容）。
    # 素材可能几百 MB，拷贝放线程池，不卡事件循环
    src_dir, dst_dir = _template_dir(src.id), _template_dir(copy.id)
    if os.path.isdir(src_dir):
        await asyncio.to_thread(shutil.copytree, src_dir, dst_dir, dirs_exist_ok=True)
    return _to_out(copy)


_ASSET_EXT = {"clip": (".mp4", ".mov"), "png": (".png",), "mov": (".mov", ".mp4")}


@router.post("/templates/{tid}/assets")
async def upload_asset(
    tid: int,
    kind: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传模板素材：前后贴片(clip) / 静态贴纸(png) / 动态特效(mov)。
    存盘后即时 ffprobe 校验（透明通道等），不合格当场删文件并报 400。
    返回 {asset, kind, url, ...meta}，asset 是模板目录内的文件名，写进 config 引用。"""
    await _get_owned_template(tid, user, db)  # ownership 校验
    if kind not in _ASSET_KINDS:
        raise HTTPException(400, f"未知素材类型 {kind}")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ASSET_EXT[kind]:
        raise HTTPException(400, f"{kind} 素材不支持 {ext or '无扩展名'} 格式")

    d = _template_dir(tid)
    os.makedirs(d, exist_ok=True)
    asset_name = f"{kind}_{uuid.uuid4().hex[:8]}{ext}"
    abs_path = os.path.join(d, asset_name)
    _label = {"clip": "贴片视频", "png": "静态贴纸", "mov": "动态特效"}[kind]
    await _save_upload_capped(file, abs_path, _ASSET_MAX_MB[kind], _label)

    try:
        meta = await asyncio.to_thread(_probe_asset, abs_path)
        _validate_asset(kind, meta, abs_path)
    except (HTTPException, ValueError) as e:
        os.remove(abs_path)  # 校验不过不留脏文件
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(400, str(e))

    extra = {}
    if kind == "clip":
        # 贴片必须存成归一化规格（720×1280@30 / H.264 / AAC 44.1k 立体声）。
        # 导出最后一步要把正片和贴片拼起来，规格对得上才走 -c copy 把正片字节原样搬过去；
        # 对不上就得整条重编码，3000k 的正片会被压成 8000k，体积翻两倍多。
        # 贴片只有几秒，这一步压的代价可以忽略。
        if not await video.at_normalize_target(abs_path):
            tmp_norm = abs_path + ".norm.mp4"
            try:
                await video.normalize_to_target(abs_path, tmp_norm)
                os.replace(tmp_norm, abs_path)
                meta = await asyncio.to_thread(_probe_asset, abs_path)
            except Exception as e:
                # 归一化失败不拦上传：素材本身是好的，大不了导出时多压一次
                if os.path.exists(tmp_norm):
                    os.remove(tmp_norm)
                log_failure("admake.template.normalize_clip", e, template_id=tid, asset=asset_name)
    elif kind == "mov":
        # 先裁掉透明边 + 分辨率封顶（剪映整块画布→只留元素，才能当小贴纸摆），再生成预览
        await asyncio.to_thread(_autocrop_dyn, abs_path, d, meta)
        base = os.path.splitext(asset_name)[0]
        preview, pkind = await asyncio.to_thread(
            _make_dyn_preview, abs_path, d, base, meta.get("duration") or 0)
        extra = {"preview": preview, "preview_kind": pkind}
    elif kind == "png":
        # 限宽 1080/限高 1920（成片 720×1280 的 1.5 倍余量，缩放放大也不糊）。
        # 贴纸只解码一次、分辨率不影响导出速度——压这步为的是模板包体积和内存。
        def _shrink():
            img = Image.open(abs_path)
            w, h = img.size
            k = min(1080 / w, 1920 / h, 1.0)
            if k >= 1.0:
                return None
            img = img.convert("RGBA").resize(
                (max(2, int(w * k)), max(2, int(h * k))), Image.LANCZOS)
            img.save(abs_path)
            return img.size
        new_size = await asyncio.to_thread(_shrink)
        if new_size:
            meta["width"], meta["height"] = new_size
    elif kind == "clip":
        # 上传即归一到成片规格(720×1280@30 H.264 + AAC 44.1k)：之后每次导出拼接
        # 都走无重编码的 copy 通道，批量导出不再逐条重复归一。归一失败不拦上传，
        # 导出阶段 merge_videos 会兜底重编码。
        base0 = os.path.splitext(asset_name)[0]
        tmp_norm = os.path.join(d, base0 + ".norm.mp4")
        final_name = base0 + ".mp4"
        try:
            await merge_videos([abs_path], tmp_norm)  # 单输入=纯归一（已达标则秒级 copy）
            if os.path.isfile(tmp_norm) and os.path.getsize(tmp_norm) > 0:
                os.remove(abs_path)
                abs_path = os.path.join(d, final_name)
                os.replace(tmp_norm, abs_path)
                asset_name = final_name
                meta = await asyncio.to_thread(_probe_asset, abs_path)
        except Exception:
            pass  # 归一失败不拦上传，导出时 merge_videos 兜底重编码
        finally:
            # 归一产出零字节/失败残留的 .norm.mp4 不留（否则 duplicate/打包会一起带走垃圾）
            try:
                os.remove(tmp_norm)
            except OSError:
                pass

    rel = os.path.relpath(abs_path, settings.UPLOAD_DIR)
    # 排查素材问题（尺寸/时长/预压效果）时按这条日志对：kind + 最终宽高 + 文件大小
    log_event("admake.template.asset", template_id=tid, kind=kind,
              width=meta.get("width"), height=meta.get("height"),
              duration=meta.get("duration"),
              size_mb=round(os.path.getsize(abs_path) / 1048576, 2))
    return {
        "asset": asset_name,
        # 磁盘上用 uuid 命名是为了防重名和特殊字符，但界面上要显示用户自己那个名字——
        # 「clip_89ff1549.mp4」谁也认不出是哪一条。原名只回给前端存进 config，不落磁盘。
        "orig_name": os.path.basename(file.filename or "") or asset_name,
        "kind": kind,
        "url": "/uploads/" + rel.replace(os.sep, "/"),
        **meta,
        **extra,
    }


def _config_assets(config: dict) -> set[str]:
    """收集 config 里引用到的全部素材文件名（前后贴片 / 贴纸 / 特效）。"""
    assets: set[str] = set()
    for key in ("intro", "outro"):
        a = (config.get(key) or {}).get("asset")
        if a:
            assets.add(a)
    for arr in ("pngs", "dyns"):
        for el in config.get(arr) or []:
            for f in (el.get("asset"), el.get("preview"), el.get("poster")):
                if f:
                    assets.add(f)
    return assets


@router.get("/templates/{tid}/package")
async def export_package(
    tid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把模板打包成 .skytpl（zip：template.json + assets/ 全部素材），供分享给同事导入。
    引用的素材文件缺失时直接报错——静默跳过会分享出一个"看着正常、导入后渲染才炸"的坏包。"""
    t = await _get_owned_template(tid, user, db)
    config = json.loads(t.config or "{}")
    d = _template_dir(tid)
    missing = [a for a in _config_assets(config) if not os.path.isfile(os.path.join(d, a))]
    if missing:
        raise HTTPException(400, f"模板素材文件缺失（{missing[0]} 等 {len(missing)} 个），请重新上传素材后再分享")

    def _build_zip() -> str:
        """打到临时文件，返回路径。

        不在内存里拼：一个模板包常有两段一百多兆的片头片尾，BytesIO 存一份、
        取值时再复制一份，峰值是包体的两倍。

        素材一律 ZIP_STORED 不压缩。mp4/mov/png 本身已经是压缩格式，DEFLATE 再压一遍
        体积几乎不变，时间全花在 CPU 上——实测 264MB 的包，压缩要 3.2 秒才吐出第一个字节。
        只有 template.json 是纯文本，值得压。"""
        fd, path = tempfile.mkstemp(suffix=".skytpl")
        os.close(fd)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("template.json",
                       json.dumps({"name": t.name, "config": config}, ensure_ascii=False),
                       compress_type=zipfile.ZIP_DEFLATED)
            for a in _config_assets(config):
                z.write(os.path.join(d, a), f"assets/{a}")
        return path

    # 几百 MB 的文件拷贝是秒级 IO，放线程池——不能卡住整个单进程后端
    path = await asyncio.to_thread(_build_zip)
    log_event("admake.template.export_pkg", template_id=tid,
              pkg_mb=round(os.path.getsize(path) / 1048576, 1))
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"template_{tid}.skytpl",
        # 响应发完才删临时文件；不挂这个会在磁盘上越积越多
        background=BackgroundTask(os.remove, path),
    )


@router.post("/templates/import", response_model=AdmakeTemplateOut, status_code=201)
async def import_package(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """导入 .skytpl 模板包：新建一个模板并把素材拷进它的目录（不覆盖已有模板）。
    包体和解压总量都有上限（防超大包吃满内存/磁盘）；素材落盘后走和上传同一套校验，
    不透明的贴纸/动效在导入这一步就拒收，而不是留到导出时渲染成方块。"""
    raw = await file.read(_PKG_MAX_MB * 1024 * 1024 + 1)
    if len(raw) > _PKG_MAX_MB * 1024 * 1024:
        raise HTTPException(400, f"模板包超过 {_PKG_MAX_MB}MB 上限")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(400, "不是有效的模板包文件（.skytpl）")
    # 解压总量上限：压缩包很小但解出来巨大的（zip bomb）当场拒
    total_unpacked = sum(i.file_size for i in z.infolist())
    if total_unpacked > _PKG_MAX_MB * 1024 * 1024:
        raise HTTPException(400, "模板包解压后超过大小上限，文件异常")
    try:
        meta = json.loads(z.read("template.json"))
    except KeyError:
        raise HTTPException(400, "模板包缺少 template.json，文件可能已损坏")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "模板包内容损坏，无法解析")

    name = (meta.get("name") or "导入的模板").strip()[:100] or "导入的模板"
    config = meta.get("config") or {}
    t = AdmakeTemplate(user_id=user.id, name=name, config=json.dumps(config, ensure_ascii=False))
    db.add(t)
    await db.flush()

    d = _template_dir(t.id)
    os.makedirs(d, exist_ok=True)

    # 只取 assets/ 下的文件、且只用 basename——防 zip 路径穿越；解几百 MB 是阻塞 IO，放线程池
    def _extract():
        for member in z.namelist():
            if member.startswith("assets/") and not member.endswith("/"):
                base = os.path.basename(member)
                if base:
                    with z.open(member) as src, open(os.path.join(d, base), "wb") as dst:
                        shutil.copyfileobj(src, dst)
    await asyncio.to_thread(_extract)

    # 导入的素材走和上传同一套校验（按 config 里的归属定类型）；坏素材/坏 config 整包拒收并清理。
    # 兜底 catch 所有异常——手改/异版本的包 config 形状不对会抛 AttributeError，
    # 只接 HTTPException 会 500 且素材目录泄漏在磁盘上
    def _verify_assets():
        checks: list[tuple[str, str]] = []
        for key, kind in (("intro", "clip"), ("outro", "clip")):
            a = (config.get(key) or {}).get("asset")
            if a:
                checks.append((a, kind))
        checks += [(p.get("asset"), "png") for p in (config.get("pngs") or []) if p.get("asset")]
        checks += [(dn.get("asset"), "mov") for dn in (config.get("dyns") or []) if dn.get("asset")]
        for asset, kind in checks:
            p = os.path.join(d, os.path.basename(asset))
            if not os.path.isfile(p):
                raise HTTPException(400, f"模板包缺少素材文件 {asset}，请让分享方重新导出")
            m = _probe_asset(p)
            _validate_asset(kind, m, p)
        return ([os.path.join(d, os.path.basename(a)) for a, k in checks if k == "clip"],
                [os.path.join(d, os.path.basename(a)) for a, k in checks if k == "mov"])
    try:
        if not isinstance(config, dict):
            raise HTTPException(400, "模板包 config 格式不正确，无法导入")
        clip_paths, dyn_paths = await asyncio.to_thread(_verify_assets)
        # 贴片归一到成片规格（与上传同待遇）：否则这个模板每次导出都走整片重编码慢路径。
        # 原文件名不动（config 引用它），归一结果原地替换
        for cp in clip_paths:
            tmp_n = cp + ".norm.mp4"
            try:
                await merge_videos([cp], tmp_n)
                if os.path.isfile(tmp_n) and os.path.getsize(tmp_n) > 0:
                    os.replace(tmp_n, cp)
            except Exception:
                pass  # 归一失败不拦导入，导出时兜底重编码
            finally:
                try:
                    os.remove(tmp_n)
                except OSError:
                    pass
        # 动态贴纸与上传同待遇：裁透明边+分辨率封顶+超 10 秒截断（分享包可能来自老版本）
        for dp in dyn_paths:
            try:
                m2 = _probe_asset(dp)
                await asyncio.to_thread(_autocrop_dyn, dp, os.path.dirname(dp), m2)
            except Exception:
                pass  # 预压失败不拦导入，导出时照常可用
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        await db.delete(t)
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(400, "模板包内容异常，无法导入")
    log_event("admake.template.import", template_id=t.id, name=name,
              pkg_mb=round(len(raw) / 1048576, 1), assets=len(_config_assets(config)))
    return _to_out(t)

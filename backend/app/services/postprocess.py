"""成片模板套用引擎：把模板配置（前后贴片 / 文字 / 静态贴纸 / 动态特效 / 闪白）
用 ffmpeg 真实合进一条成片。

两段式，避开"多段拼接音频对齐"这类坑（KNOWLEDGE #1）：
  阶段①  把文字/贴纸/特效叠加到正片本身（同时长，音频原样保留），并在需要接贴片的
         接缝处做闪白 → main_decorated.mp4（已是 720×1280 归一化规格）。
  阶段②  若配了前/后贴片，用 video.merge_videos([intro, main_decorated, outro]) 拼接
         （它负责把贴片归一化到同规格 + 补静音 + concat，已被打磨过）。

坐标约定：config 里 x/y 是元素中心占画布的百分比（基准 720×1280），scale/rot(度)/flip
由前端拖拽画布产出，这里换算成 ffmpeg overlay 的像素表达式。
"""
import asyncio
import math
import os
import re
import shutil
import subprocess
import uuid

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from app.services.video import (
    merge_videos, get_video_info, at_normalize_target, normalize_to_target)

# 字体：优先用打包字体（app/fonts/ 下放免费商用花字 .ttf），缺则退系统中文字体。
# 最终兜底一定是中文字体——绝不退 Pillow 拉丁默认字体，否则中文会渲成缺字方框。
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")
_HEITI = "/System/Library/Fonts/STHeiti Medium.ttc"  # 系统必有的中文黑体，做最终兜底
_FONTS = {
    # 中文
    "宋体": ["Songti.ttf", "/System/Library/Fonts/Supplemental/Songti.ttc"],
    "黑体": ["Heiti.ttf", _HEITI],
    "圆润体": ["Yuan.ttf", "/System/Library/Fonts/Hiragino Sans GB.ttc"],
    # 海外拉丁展示体（Google Fonts，SIL OFL 免费商用；打包在 app/fonts/）
    "Anton": ["Anton.ttf"],
    "Bebas": ["BebasNeue.ttf"],
    "Poppins": ["Poppins.ttf"],
    "Alfa Slab": ["AlfaSlab.ttf"],
    "Bangers": ["Bangers.ttf"],
    "Lobster": ["Lobster.ttf"],
    "Pacifico": ["Pacifico.ttf"],
    "Playfair": ["PlayfairDisplay.ttf"],
}
_FONT_SIZES = {"宋体": 66, "黑体": 58, "圆润体": 58,
               "Anton": 68, "Bebas": 74, "Poppins": 56, "Alfa Slab": 56,
               "Bangers": 66, "Lobster": 70, "Pacifico": 62, "Playfair": 62}
_COLORS = {
    "金": (255, 215, 0, 255),
    "白": (255, 255, 255, 255),
    "红": (225, 29, 42, 255),
    "粉": (255, 120, 170, 255),
    "蓝": (60, 140, 255, 255),
    "黑": (20, 20, 20, 255),
}
# 旧样式名 → (字体, 颜色)，兼容早期模板
_LEGACY_STYLE = {
    "金色花体": ("宋体", "金"), "白字黑边": ("黑体", "白"),
    "白字红底": ("黑体", "红"), "简约黑体": ("黑体", "白"),
}


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _period_range(period: str, dur: float) -> tuple[float, float]:
    m = re.match(r"前\s*(\d+)\s*秒", period or "")
    if m:
        return (0.0, float(m.group(1)))
    return (0.0, round(dur + 1, 2))  # 全程 / 循环整段


def _font_path(font_key: str) -> str:
    for c in _FONTS.get(font_key, _FONTS["黑体"]):
        p = c if os.path.isabs(c) else os.path.join(_FONT_DIR, c)
        if os.path.isfile(p):
            return p
    return _HEITI


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    for p in (path, _HEITI):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _ramp(c: tuple) -> tuple:
    """从主色算出立体特效用的一组色：高光 / 主色 / 暗部 / 挤出 / 描边。"""
    r, g, b = c[:3]
    light = tuple(int(v + (255 - v) * 0.75) for v in (r, g, b))
    deep = tuple(int(v * 0.42) for v in (r, g, b))
    extr = tuple(int(v * 0.5) for v in (r, g, b))
    outline = tuple(int(v * 0.2) for v in (r, g, b))
    return light, (r, g, b), deep, extr, outline


def _render_bevel(text: str, font: ImageFont.FreeTypeFont, fill: tuple) -> Image.Image:
    """立体金属特效：金属渐变填充 + 立体挤出 + 细描边 + 投影。支持 \\n 多行（居中）。
    2× 超采样渲染后缩回目标尺寸——按目标字号 1:1 直渲边缘会糊；厚描边/大发光的旧配方
    会把字腔（a/e/o 的洞）糊死，改成细描边 + 短投影，小字号也保持清晰。"""
    S = 2  # 超采样倍数
    big = font.font_variant(size=font.size * S)
    sp = int(big.size * 0.15)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    bb = probe.multiline_textbbox((0, 0), text, font=big, spacing=sp)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    pad = int(big.size * 0.35) + 8 * S
    W, H = tw + pad * 2, th + pad * 2
    ox, oy = pad - bb[0], pad - bb[1]
    light, base, deep, extr, outline = _ramp(fill)

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).multiline_text((ox, oy), text, font=big, fill=255,
                                        align="center", spacing=sp)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    def _stamp(color, mask_img, off=(0, 0)):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.paste(color, off, mask_img)
        return Image.alpha_composite(canvas, layer)

    # ① 短投影（替代旧的大半径外发光——那层雾是"糊成一坨"的主因之一）
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 110), (0, 3 * S), mask)
    canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(2 * S)))
    # ② 细描边（旧 11px MaxFilter 会填死字腔；2×下 7 ≈ 成品 1.5px）
    canvas = _stamp(outline + (255,), mask.filter(ImageFilter.MaxFilter(7)))
    # ③ 立体挤出（右下短斜切，2×下 6 ≈ 成品 3px）
    for d in range(3 * S, 0, -1):
        canvas = _stamp(extr + (255,), mask, (d, d))

    # ④ 竖向金属渐变填充：上亮下深，高光带压窄到上 35% 更有金属感
    col = Image.new("RGBA", (1, H))
    for y in range(H):
        t = min(1, max(0, (y - oy) / max(1, th)))
        if t < 0.35:
            c = tuple(int(light[j] + (base[j] - light[j]) * (t / 0.35)) for j in range(3))
        else:
            c = tuple(int(base[j] + (deep[j] - base[j]) * ((t - 0.35) / 0.65)) for j in range(3))
        col.putpixel((0, y), c + (255,))
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    grad.paste(col.resize((W, H)), (0, 0), mask)
    canvas = Image.alpha_composite(canvas, grad)
    # ⑤ 缩回目标尺寸：LANCZOS 下采样，边缘干净
    return canvas.resize((W // S, H // S), Image.LANCZOS)


# 拉丁展示体（无中文字形），给中文用会出方框，需自动回退中文字体
_LATIN_FONTS = {"Anton", "Bebas", "Poppins", "Alfa Slab", "Bangers", "Lobster", "Pacifico", "Playfair"}


def _has_cjk(s: str) -> bool:
    return any("一" <= c <= "鿿" or "㐀" <= c <= "䶿" for c in s or "")


# 文字安全宽度：成片宽 720 的 90%。超出先缩字号（到 70% 底线），再折两行（到 50% 底线），
# 仍超宽截断加省略号。前端预览用同一套规则（template-editor.html::fitText），所见即所得。
_MAX_TEXT_W = 648


def _text_width(text: str, font: ImageFont.FreeTypeFont) -> int:
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    return max(
        (probe.textbbox((0, 0), ln, font=font)[2] for ln in text.split("\n")), default=0)


def _split_two(text: str) -> tuple[str, str]:
    """折两行的断点：英文取最靠中间的空格；中文/无空格对半断。"""
    t = text.strip()
    spaces = [i for i, c in enumerate(t) if c == " "]
    if spaces:
        mid = min(spaces, key=lambda i: abs(i - len(t) / 2))
        return t[:mid].rstrip(), t[mid + 1:].lstrip()
    half = (len(t) + 1) // 2
    return t[:half], t[half:]


def _fit_text(text: str, font_path: str, base_size: int) -> tuple[str, int]:
    """先缩后换行。返回 (最终文本[可能含\\n], 字号)。"""
    w = _text_width(text, _load_font(font_path, base_size))
    if w <= _MAX_TEXT_W:
        return text, base_size
    k = _MAX_TEXT_W / w
    if k >= 0.7:  # 缩字号就装得下：保持单行海报感
        return text, max(12, int(base_size * k))
    # 折两行，从 70% 底线继续按最长行微缩到 50% 底线
    l1, l2 = _split_two(text)
    two = f"{l1}\n{l2}"
    size = int(base_size * 0.7)
    floor = max(12, int(base_size * 0.5))
    for _ in range(4):
        lw = _text_width(two, _load_font(font_path, size))
        if lw <= _MAX_TEXT_W:
            return two, size
        size = max(floor, int(size * _MAX_TEXT_W / lw))
        if size == floor:
            break
    # 50% 底线仍超宽：逐字截断更宽的那一行 + 省略号（长单词可能落在第一行，两行都要能截）
    f = _load_font(font_path, size)
    while _text_width(two, f) > _MAX_TEXT_W and (len(l1) > 2 or len(l2) > 2):
        if _text_width(l1, f) >= _text_width(l2, f) and len(l1) > 2:
            l1 = l1[:-2] + "…"
        elif len(l2) > 2:
            l2 = l2[:-2] + "…"
        else:
            break
        two = f"{l1}\n{l2}"
    return two, size


def _render_text_png(text: str, font_key: str, color_key: str, effect: str, out_png: str) -> None:
    """把一条文字渲染成透明 PNG（按目标字号直接渲，不拉伸，避免糊）。
    effect='立体' 走金属立体特效；否则平面填充 + 自动描边（深色配白边、其余配深边）。
    超宽文本先缩后换行（_fit_text），最多两行居中。"""
    if font_key in _LEGACY_STYLE:  # 兼容旧模板的样式名
        font_key, color_key = _LEGACY_STYLE[font_key]
    # 拉丁字体遇中文自动换中文字体——绝不出方框
    if font_key in _LATIN_FONTS and _has_cjk(text or ""):
        font_key = "黑体"
    fpath = _font_path(font_key)
    text, size = _fit_text(text or " ", fpath, _FONT_SIZES.get(font_key, 58))
    font = _load_font(fpath, size)
    fill = _COLORS.get(color_key, _COLORS["白"])

    # 立体特效依赖矢量字体（font_variant/size）；两级字体都加载失败退回位图默认字体时
    # 没有这些能力，降级平面渲染——宁可不立体，不能让整条模板套用崩掉
    if effect == "立体" and isinstance(font, ImageFont.FreeTypeFont):
        _render_bevel(text, font, fill).save(out_png)
        return

    stroke = (255, 255, 255, 255) if sum(fill[:3]) < 180 else (20, 20, 20, 220)
    stroke_w, padx, pady = 4, 20, 12
    sp = int(size * 0.15)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    bbox = probe.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke_w, spacing=sp)
    cw = (bbox[2] - bbox[0]) + padx * 2
    ch = (bbox[3] - bbox[1]) + pady * 2
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    ImageDraw.Draw(img).multiline_text(
        (padx - bbox[0], pady - bbox[1]), text, font=font,
        fill=fill, stroke_width=stroke_w, stroke_fill=stroke,
        align="center", spacing=sp)
    img.save(out_png)


def _resolve_vars(text: str, fields: dict) -> str:
    return (str(text or "")
            .replace("{剧名}", fields.get("title") or "")
            .replace("{署名}", fields.get("editor_alias") or "")
            .replace("{平台}", fields.get("target_platform") or ""))


def _overlay_subchain(inp: int, el: dict, label: str) -> tuple[str, str]:
    """构造一个静态元素（文字/PNG）的变换子链 + overlay 表达式。
    返回 (子链片段, overlay 表达式)。"""
    sub = f"[{inp}:v]format=rgba"
    if el.get("flip"):
        sub += ",hflip"
    scale = float(el.get("scale", 1) or 1)
    if abs(scale - 1) > 0.01:
        sub += f",scale=iw*{scale}:-1:flags=lanczos"
    rot = float(el.get("rot", 0) or 0)
    if abs(rot) > 0.5:
        rad = rot * math.pi / 180
        sub += f",rotate={rad:.5f}:c=none:ow=rotw({rad:.5f}):oh=roth({rad:.5f})"
    sub += f"[{label}]"
    xr = float(el.get("x", 50)) / 100
    yr = float(el.get("y", 50)) / 100
    return sub, f"overlay=x=W*{xr:.4f}-w/2:y=H*{yr:.4f}-h/2"


async def _normalized_clip(path: str) -> str:
    """贴片素材规格不对就先压成归一化规格，压好的存旁边复用。

    正片和贴片规格一致时，merge_videos 走 `-c copy` 把正片字节原样搬过去；
    只要有一条对不上，整条正片就要被重编码，3000k 的成片会被压成 8000k、体积翻两倍多。

    2026-08-08 起，贴片在上传时就归一化了，这里兜的是那之前存进来的老素材——
    库里已有的模板都是 1706×2560 的片尾，不兜的话它们导出的片子会一直偏大。
    转好的结果落在 `<原名>.norm.mp4`，同一个素材只压一次。
    """
    if not os.path.isfile(path):
        return path
    try:
        if await at_normalize_target(path):
            return path
        norm = os.path.splitext(path)[0] + ".norm.mp4"
        if os.path.isfile(norm) and await at_normalize_target(norm):
            return norm
        await normalize_to_target(path, norm)
        return norm
    except Exception:
        # 归一化失败就用原素材：拼出来的片子偏大，但不能因此导不出片
        return path


async def apply_template(input_video: str, config: dict, project_fields: dict,
                         asset_dir: str, out_path: str) -> str:
    """把模板套用到一条成片，产出 out_path。config 见模块注释的结构。"""
    config = config or {}
    texts = config.get("texts") or []
    pngs = config.get("pngs") or []
    dyns = config.get("dyns") or []
    intro = (config.get("intro") or {}).get("asset")
    outro = (config.get("outro") or {}).get("asset")
    transition = config.get("transition")  # white=快闪白 / black=渐黑 / none

    info = await get_video_info(input_video)
    main_dur = float(info.get("duration") or 0.0)

    work = os.path.join(os.path.dirname(os.path.abspath(out_path)), f".tpl_{uuid.uuid4().hex[:8]}")
    os.makedirs(work, exist_ok=True)
    try:
        # 阶段①：叠加到正片
        cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", input_video]
        fc, idx = [], 1
        text_ovs, png_ovs, dyn_ovs = [], [], []

        for i, t in enumerate(texts):
            png = os.path.join(work, f"text_{i}.png")
            _render_text_png(_resolve_vars(t.get("content"), project_fields),
                             t.get("style"), t.get("color"), t.get("effect"), png)
            cmd += ["-i", png]
            text_ovs.append((idx, "img", t)); idx += 1
        for p in pngs:
            cmd += ["-i", os.path.join(asset_dir, p["asset"])]
            png_ovs.append((idx, "img", p)); idx += 1
        for d in dyns:
            cmd += ["-stream_loop", "-1", "-i", os.path.join(asset_dir, d["asset"])]
            dyn_ovs.append((idx, "dyn", d)); idx += 1

        # 叠放顺序（从底到顶）：动效 < 静态贴纸 < 文字——文字永远在最上、不被效果盖住
        overlays = dyn_ovs + png_ovs + text_ovs

        base = "0:v"
        for k, (i, kind, el) in enumerate(overlays):
            ov, nb = f"ov{k}", f"b{k}"
            a, b = _period_range(el.get("period"), main_dur)
            # 静态贴纸(img)和动态特效(dyn)同样处理：按 x/y% 定位 + scale/rotate/flip。
            # 动效的循环靠输入侧 -stream_loop -1；整块画布素材默认居中原尺寸即满屏。
            sub, ov_expr = _overlay_subchain(i, el, ov)
            fc.append(sub)
            fc.append(f"[{base}][{ov}]{ov_expr}:enable='between(t,{a},{b})'[{nb}]")
            base = nb

        # 接缝转场（快闪白 / 渐黑）：只在会接贴片的接缝处（片头后 / 片尾前）
        _t_color = {"white": "white", "black": "black"}.get(transition)
        _t_dur = 0.3 if transition == "white" else 0.6
        flash_ops = []
        if _t_color and intro:
            flash_ops.append(f"fade=t=in:st=0:d={_t_dur}:color={_t_color}")
        if _t_color and outro and main_dur > _t_dur:
            flash_ops.append(f"fade=t=out:st={round(main_dur - _t_dur, 2)}:d={_t_dur}:color={_t_color}")
        if flash_ops:
            fc.append(f"[{base}]{','.join(flash_ops)}[vout]")
            vout = "vout"
        else:
            vout = base

        decorated = os.path.join(work, "main_decorated.mp4")
        if fc:
            cmd += ["-filter_complex", ";".join(fc), "-map", f"[{vout}]", "-map", "0:a:0?"]
        else:
            # 无任何叠加/闪白：直接归一化重编码（保持规格一致，供阶段②拼接）
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        tail = ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
        # 动效用 -stream_loop -1 是无限流，必须按正片时长封顶输出，否则 ffmpeg 不会结束
        if main_dur > 0:
            tail += ["-t", f"{main_dur:.3f}"]
        tail += [decorated]
        # 硬编 videotoolbox 3000k（与 export_final_video 同一套参数与理由，2026-07-21 已验证
        # 色彩一致、快约 4.8×、CPU 减半）；不可用/并发超限时降级 libx264 CRF 软编。
        cmd_hw = cmd + ["-c:v", "h264_videotoolbox", "-b:v", "3000k", "-allow_sw", "1",
                        "-pix_fmt", "yuv420p"] + tail
        cmd_sw = cmd + ["-c:v", "libx264", "-crf", "20", "-preset", "medium",
                        "-pix_fmt", "yuv420p"] + tail

        def _run():
            proc = subprocess.run(cmd_hw, capture_output=True, text=True)
            if proc.returncode != 0:
                proc = subprocess.run(cmd_sw, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"模板叠加失败：{proc.stderr[-400:]}")
        await asyncio.to_thread(_run)

        # 阶段②：拼前后贴片
        segs = []
        if intro:
            segs.append(await _normalized_clip(os.path.join(asset_dir, intro)))
        segs.append(decorated)
        if outro:
            segs.append(await _normalized_clip(os.path.join(asset_dir, outro)))

        if len(segs) == 1:
            shutil.move(decorated, out_path)
        else:
            await merge_videos(segs, out_path)
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)

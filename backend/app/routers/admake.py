"""AdMake 广告素材平台路由。"""
import asyncio
import json
import math
import os
import pathlib
import re
import shutil
import time as _time
import uuid
from collections import defaultdict
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks, Query
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, update, or_, false
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import User
from app.models.admake import AdmakeProject, AdmakeSource, AdmakePart, AdmakeMark, AdmakeExport, AdmakeTemplate
from app.schemas.admake import (
    AdmakeProjectCreate, AdmakeProjectUpdate,
    AdmakeProjectOut, AdmakeProjectDetailOut, AdmakeTaskOut,
    AdmakeSourceOut, AdmakePartOut,
    AdmakeMarkIn, AdmakeMarkBatchSave, AdmakeMarkOut,
    AdmakeExportRequest, AdmakeExportOut,
)
from app.core.deps import get_current_user
from app.core.config import settings
from app.core.logs import log_failure, log_event
from app.services import video as vid_svc
from app.services.transition_detect import detect_transitions

router = APIRouter(prefix="/admake", tags=["AdMake"])

# AI 分析入口密钥预检的探测超时（秒）——只读探测，要快进快出
OSS_PRECHECK_TIMEOUT_SEC = 10
HAPPYHORSE_PRECHECK_TIMEOUT_SEC = 10

# AI 分析流水线并发上限：Part N 在代理侧分析（纯网络等待 1~4 分钟）时，
# Part N+1 可并行压缩+上传。Dscript 代理容量未确认，保守 2 路，勿随意调大。
AI_ANALYZE_CONCURRENCY = 4

# Gemini 代理源站间歇性宕机（Cloudflare 502 / 5xx / 连接失败）时的退避重试节奏（秒）。
# 只对 POST 代理这一步重试，复用已上传的 OSS 链接，不重复压缩上传。跨过源站的短暂
# 宕机窗口即可自动恢复；对小时级的持续宕机无能为力（那要修源站本身）。可按需调。
GEMINI_PROXY_RETRY_BACKOFF_SEC = [30, 60, 120]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    return re.sub(r'[^\w一-鿿\-]', '_', text).strip('_')[:50]

# 导出文件命名规则里禁止的字符：文件系统保留字符 + "_"（拼接字段用的分隔符，
# 不能出现在字段内容里，否则解析/展示会跟分隔符混淆）。
_NAMING_BANNED_RE = re.compile(r'[\/\\*?"<>|_]')

# 项目标题进文件名时的上限。另一份在 electron-app/renderer/tools/ad-make/index.html
# 的 TITLE_MAX_LEN（输入框校验），改要一起改，否则界面放进来的标题会在导出时被悄悄截短。
# 100 是按文件系统余量定的：成片旁边还有一个同名 .log.txt，整个文件名是
# {剧名}_SkyArt_{月.日}_{署名}_{序号}.log.txt，固定部分最多占 47 个字符，
# 而 APFS 单个文件名上限是 255 个字符（按字符数算，中文英文一样，实测确认）。
_TITLE_MAX_LEN = 100


def _validate_naming_field(value: str, field_label: str, max_len: int, allow_mid_colon: bool) -> None:
    """校验剧名 / 导出文件命名：非法字符全位置禁止；冒号规则按字段区分（剧名允许中间出现）。"""
    if len(value) > max_len:
        raise HTTPException(400, f"{field_label}不能超过 {max_len} 个字符")
    bad = _NAMING_BANNED_RE.search(value)
    if bad:
        raise HTTPException(400, f'{field_label}包含不支持的符号「{bad.group()}」，请修改后重试')
    starts_colon = value.startswith(':') or value.startswith('：')
    ends_colon = value.endswith(':') or value.endswith('：')
    if allow_mid_colon:
        if starts_colon or ends_colon:
            raise HTTPException(400, f'{field_label}不能以「：」开头或结尾')
    elif ':' in value or '：' in value:
        raise HTTPException(400, f'{field_label}不能包含「：」')

def _project_parts_dir(project_id: int, title: str) -> str:
    d = os.path.join(settings.UPLOAD_DIR, "AdMake", "Parts", f"project_{project_id}_{_slug(title)}")
    os.makedirs(d, exist_ok=True)
    return d

def _project_sources_dir(project_id: int, title: str) -> str:
    d = os.path.join(settings.UPLOAD_DIR, "AdMake", "Source", f"project_{project_id}_{_slug(title)}")
    os.makedirs(d, exist_ok=True)
    return d

def _project_exports_dir(project_id: int, title: str, export_path: str | None = None) -> str:
    """导出目录。export_path 非空 = 用户自定义本地目录，在其下按剧名自动建子文件夹
    （<自定义目录>/<剧名>/），每个项目各占一个；建不出来（路径无效/无权限）时回退默认目录，
    绝不因此让导出失败。无自定义目录时用 App 数据目录内的默认目录（内部存储，名带 id 防同名冲突）。"""
    if export_path and export_path.strip():
        folder = _slug(title) or f"project_{project_id}"  # 剧名做文件夹名，空标题兜底
        d = os.path.join(export_path.strip(), folder)
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception as e:
            log_failure("admake.export_path", e, export_path=d)
            # 落到默认目录兜底
    d = os.path.join(settings.UPLOAD_DIR, "AdMake", "Export", f"project_{project_id}_{_slug(title)}")
    os.makedirs(d, exist_ok=True)
    return d

def _export_rel(final: str) -> str:
    """成片入库路径：在 UPLOAD_DIR 内存相对路径（供 /uploads 服务），
    在自定义外部目录则存绝对路径（前端 openExportDir 按是否绝对路径分别处理）。"""
    upload_abs = os.path.abspath(settings.UPLOAD_DIR)
    final_abs = os.path.abspath(final)
    if final_abs.startswith(upload_abs + os.sep):
        return os.path.relpath(final_abs, upload_abs)
    return final_abs

def _get_batch_dir(project_id: int, title: str, export_path: str | None = None) -> str:
    exports_dir = _project_exports_dir(project_id, title, export_path)
    # 自定义导出目录：剧名文件夹就是最终一层，成片直接放进去（文件名自带日期+编号，够区分）。
    # 日期批次子文件夹只给 App 内部默认目录用。注意自定义路径无效时上面会回退默认目录，
    # 这里要确认真落在了自定义目录下才跳过批次层。
    if export_path and export_path.strip():
        custom_root = os.path.abspath(export_path.strip())
        if os.path.abspath(exports_dir).startswith(custom_root + os.sep):
            return exports_dir
    today = datetime.now().strftime('%Y%m%d')
    existing = [d for d in os.listdir(exports_dir)
                if os.path.isdir(os.path.join(exports_dir, d)) and d.startswith(today + '_')]
    batch_num = len(existing) + 1
    batch_dir = os.path.join(exports_dir, f"{today}_{batch_num:03d}")
    os.makedirs(batch_dir, exist_ok=True)
    return batch_dir

def _ai_tmp_dir() -> str:
    d = os.path.join(settings.UPLOAD_DIR, "AdMake", "ai_tmp")
    os.makedirs(d, exist_ok=True)
    return d

def _project_upload_log_dir(project_id: int, title: str) -> str:
    d = os.path.join(settings.UPLOAD_DIR, "AdMake", "Upload", f"project_{project_id}_{_slug(title)}")
    os.makedirs(d, exist_ok=True)
    return d

def _get_log_batch_path(project_id: int, title: str) -> str:
    log_dir = _project_upload_log_dir(project_id, title)
    today = datetime.now().strftime('%Y%m%d')
    existing = [f for f in os.listdir(log_dir) if f.startswith(today + '_') and f.endswith('.txt')]
    batch_num = len(existing) + 1
    return os.path.join(log_dir, f"{today}_{batch_num:03d}.txt")


async def _get_project(pid: int, user: User, db: AsyncSession) -> AdmakeProject:
    res = await db.execute(
        select(AdmakeProject).where(AdmakeProject.id == pid, AdmakeProject.user_id == user.id)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在或无权限")
    return p


async def _get_part(part_id: int, user: User, db: AsyncSession) -> AdmakePart:
    res = await db.execute(
        select(AdmakePart)
        .join(AdmakeProject, AdmakePart.project_id == AdmakeProject.id)
        .where(AdmakePart.id == part_id, AdmakeProject.user_id == user.id)
    )
    part = res.scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part 不存在或无权限")
    return part


def _eff_dur(src: AdmakeSource) -> float:
    """源视频扣掉片尾板之后的可用时长。

    tail_trim 是 transition_detect 检出的集尾定格/缓放段长度，检不到时为 0。
    分组、切片、拼接一律按这个长度算，片尾板就不会进 Part——它既污染 AI 分析
    （模型会把定格画面当剧情），也会在成片里留下一段静止。
    """
    return max(0.0, src.duration_sec - (src.tail_trim or 0.0))


def _group_into_parts(sources: list[AdmakeSource]) -> list[list[AdmakeSource]]:
    MAX_SEC = 6 * 60
    groups: list[list[AdmakeSource]] = []
    cur: list[AdmakeSource] = []
    cur_dur = 0.0
    for src in sorted(sources, key=lambda s: s.sort_order):
        d = _eff_dur(src)
        if not cur:
            cur.append(src); cur_dur += d
        elif cur_dur + d <= MAX_SEC:
            cur.append(src); cur_dur += d
        else:
            groups.append(cur); cur = [src]; cur_dur = d
    if cur:
        groups.append(cur)
    return groups


def _build_segments(marks: list[AdmakeMark], mode: str) -> list[tuple[float, float]]:
    """Convert marks to (start_sec, end_sec) segment list for the given mode."""
    grouped: dict[int, dict[str, float]] = defaultdict(dict)
    for m in marks:
        grouped[m.group_index][m.mark_type] = m.time_sec

    complete = []
    for gi in sorted(grouped.keys()):
        g = grouped[gi]
        if all(k in g for k in ("highlight_start", "highlight_end", "hook_start", "hook_end")):
            complete.append(g)

    if not complete:
        return []

    segments: list[tuple[float, float]] = []

    if mode == "highlight_shunjian":
        g = complete[0]
        segments.append((g["highlight_start"], g["highlight_end"]))
        if g["hook_start"] > g["highlight_end"]:
            segments.append((g["highlight_end"], g["hook_start"]))
        segments.append((g["hook_start"], g["hook_end"]))

    elif mode == "xuanyi_daozhi":
        g = complete[0]
        segments.append((g["hook_start"], g["hook_end"]))
        segments.append((g["highlight_start"], g["highlight_end"]))

    elif mode == "shuanggaoguang":
        for g in complete[:2]:
            segments.append((g["highlight_start"], g["highlight_end"]))
            if g["hook_start"] > g["highlight_end"]:
                segments.append((g["highlight_end"], g["hook_start"]))
            segments.append((g["hook_start"], g["hook_end"]))

    return segments


async def _collect_all_marks(project_id: int, db: AsyncSession) -> tuple[list[dict], list[dict]]:
    """从项目所有就绪 Part 中收集高光和钩子，返回 (all_highlights, all_hooks)。"""
    parts_res = await db.execute(
        select(AdmakePart)
        .where(AdmakePart.project_id == project_id, AdmakePart.status == "ready")
        .order_by(AdmakePart.part_index)
    )
    parts = parts_res.scalars().all()

    all_highlights: list[dict] = []
    all_hooks:      list[dict] = []

    for part in parts:
        if not part.file_path:
            continue
        marks_res = await db.execute(select(AdmakeMark).where(AdmakeMark.part_id == part.id))
        marks = marks_res.scalars().all()

        hl_s = {m.group_index: m for m in marks if m.mark_type == "highlight_start"}
        hl_e = {m.group_index: m for m in marks if m.mark_type == "highlight_end"}
        hk_s = {m.group_index: m for m in marks if m.mark_type == "hook_start"}
        hk_e = {m.group_index: m for m in marks if m.mark_type == "hook_end"}

        for gi in sorted(hl_s):
            if gi in hl_e and hl_e[gi].time_sec > hl_s[gi].time_sec:
                all_highlights.append({
                    "part_id":       part.id,
                    "part_name":     part.name,
                    "part_index":    part.part_index,
                    "file_path":     part.file_path,
                    "part_duration": part.duration_sec,
                    "start":         hl_s[gi].time_sec,
                    "end":           hl_e[gi].time_sec,
                    "score":         hl_s[gi].score or 0,
                })
        for gi in sorted(hk_s):
            if gi in hk_e and hk_e[gi].time_sec > hk_s[gi].time_sec:
                all_hooks.append({
                    "part_id":       part.id,
                    "part_name":     part.name,
                    "part_index":    part.part_index,
                    "file_path":     part.file_path,
                    "part_duration": part.duration_sec,
                    "start":         hk_s[gi].time_sec,
                    "end":           hk_e[gi].time_sec,
                    "score":         hk_s[gi].score or 0,
                })

    return all_highlights, all_hooks


def _between_segments(hl: dict, hk: dict, parts_ordered: list[dict] | None) -> list[dict]:
    """高光结束到钩子开始之间的原片内容，按 Part 边界切成若干段。

    代码里管这些段叫 whitespace（白场），但它们不是空白，是原片连续播放的剧情——
    一条 5 分钟的高光顺剪里这部分占到八九成时长。高光顺剪、双高光、悬疑倒置三种模式
    都要接这一段，写在一处，改规则时不会漏掉某个模式。
    """
    out: list[dict] = []
    if hl["part_id"] == hk["part_id"]:
        if hk["start"] > hl["end"]:
            out.append({"file_path": hl["file_path"], "start": hl["end"],
                        "end": hk["start"], "label": "whitespace", "part_name": hl["part_name"]})
        return out
    # 高光结束 → 高光所在 Part 末尾
    if hl["part_duration"] > hl["end"]:
        out.append({"file_path": hl["file_path"], "start": hl["end"],
                    "end": hl["part_duration"], "label": "whitespace", "part_name": hl["part_name"]})
    # 两个 Part 之间的整集
    if parts_ordered:
        hl_idx = hl.get("part_index", -1)
        hk_idx = hk.get("part_index", -1)
        for p in parts_ordered:
            if hl_idx < p["part_index"] < hk_idx and p["part_duration"] > 0:
                out.append({"file_path": p["file_path"], "start": 0.0,
                            "end": p["part_duration"], "label": "whitespace",
                            "part_name": p["part_name"]})
    # 钩子所在 Part 开头 → 钩子开始
    if hk["start"] > 0:
        out.append({"file_path": hk["file_path"], "start": 0.0,
                    "end": hk["start"], "label": "whitespace", "part_name": hk["part_name"]})
    return out


def _plan_export(
    mode: str,
    pairs: list[tuple[dict, dict]],
    parts_ordered: list[dict] | None = None,
) -> list[dict]:
    """
    从若干 (highlight, hook) 配对构建导出片段计划。
    每个 segment: {file_path, start, end, label, part_name, score?}
    label: "highlight" | "whitespace" | "hook"（白场没有 score）

    parts_ordered: 全部 ready Part 的有序列表，用于填充跨 Part 时的中间 Part 白场。

    双高光有两组配对，成片里先播哪一组按高光在剧里的先后定。不排的话顺序取决于配对
    枚举时的评分高低，第 10 集的高光会拼在第 1 集前面，"顺剪"就不顺了。
    """
    segments: list[dict] = []

    if len(pairs) > 1:
        pairs = sorted(pairs, key=lambda pr: (pr[0].get("part_index", 0), pr[0]["start"]))

    for pair in pairs:
        # 顺剪类一组两个标记，悬疑倒置一组三个（多一个开场悬念）
        hl, hk = (pair[1], pair[2]) if len(pair) == 3 else pair
        same = hl["part_id"] == hk["part_id"]

        if mode in ("highlight_shunjian", "shuanggaoguang"):
            segments.append({"file_path": hl["file_path"], "start": hl["start"],
                              "end": hl["end"], "label": "highlight", "part_name": hl["part_name"],
                              "score": hl["score"]})
            segments.extend(_between_segments(hl, hk, parts_ordered))
            segments.append({"file_path": hk["file_path"], "start": hk["start"],
                              "end": hk["end"], "label": "hook", "part_name": hk["part_name"],
                              "score": hk["score"]})

        elif mode == "xuanyi_daozhi":
            # 一组是三个标记：开场悬念（取自更晚的剧情）→ 高光 → 中间剧情 → 收尾钩子。
            # 老实现只有「悬念 + 一个高光」两段，成片物理上做不过一分十几秒，目标时长
            # 填 3 分还是 10 分都撞在同一个天花板上。补上中间剧情之后长度连续可调。
            hk_open, hl, hk_end = pair
            segments.append({"file_path": hk_open["file_path"], "start": hk_open["start"],
                              "end": hk_open["end"], "label": "hook", "part_name": hk_open["part_name"],
                              "score": hk_open["score"]})
            segments.append({"file_path": hl["file_path"], "start": hl["start"],
                              "end": hl["end"], "label": "highlight", "part_name": hl["part_name"],
                              "score": hl["score"]})
            segments.extend(_between_segments(hl, hk_end, parts_ordered))
            segments.append({"file_path": hk_end["file_path"], "start": hk_end["start"],
                              "end": hk_end["end"], "label": "hook", "part_name": hk_end["part_name"],
                              "score": hk_end["score"]})

    return segments


def _estimate_pair_duration(hl: dict, hk: dict, parts_ordered: list[dict]) -> float | None:
    """
    预估 (highlight, hook) 配对后输出视频的总时长（秒）。
    若配对本身不合法则返回 None。

    highlight_shunjian 的总时长 = 高光段 + 白场（高光末尾→钩子开头，含中间 Part）+ 钩子段。
    """
    if hl["part_id"] == hk["part_id"]:
        if hk["start"] <= hl["end"]:
            return None
        return hk["end"] - hl["start"]

    hl_idx = hl.get("part_index", -1)
    hk_idx = hk.get("part_index", -1)
    if hk_idx < hl_idx:
        return None

    intermediate_dur = sum(
        p["part_duration"] for p in parts_ordered
        if hl_idx < p["part_index"] < hk_idx
    )
    # hl_part 贡献：从 hl_start 到 hl_part 末尾；hk_part 贡献：从 0 到 hk_end
    return (hl["part_duration"] - hl["start"]) + intermediate_dur + hk["end"]


# 扣掉模板贴片后留给正片的最短目标。历史模板里存过 188 秒的"片尾"，直接相减会得到
# 负数目标，选材会一路挑到最短的那个配对上，成片只剩几秒的高光。
_MIN_BODY_SEC = 30.0

_EXPORT_MODES = ("highlight_shunjian", "xuanyi_daozhi", "shuanggaoguang")
_MAX_EXPORT_COUNT = 30   # 与导出面板里"最多 30"一致


async def _template_sticker_duration(template_id: int | None, db: AsyncSession) -> float:
    """模板前后贴片的总时长（秒）。用户在界面上选的目标时长指成片总长，而贴片是整段
    接在头尾的，选材时得先把这部分让出来。模板已删或 config 是脏数据时按 0 算——
    选材偏长好过导出直接失败。"""
    if not template_id:
        return 0.0
    template = (await db.execute(
        select(AdmakeTemplate).where(AdmakeTemplate.id == template_id)
    )).scalar_one_or_none()
    if not template:
        return 0.0
    try:
        config = json.loads(template.config or "{}")
    except (ValueError, TypeError):
        return 0.0
    total = 0.0
    for key in ("intro", "outro"):
        clip = config.get(key)
        if not isinstance(clip, dict):
            continue
        try:
            total += float(clip.get("duration") or 0.0)
        except (ValueError, TypeError):
            continue
    return total


def _mark_key(m: dict) -> tuple:
    """标识一段素材，供批次内去重/均衡使用。同类段重叠超一半（IoU>0.5）会被
    _cluster_marks 归入同簇（共享 _ckey）——近似相同的两段在配对里视为同一素材，
    避免同批多条成片用着几乎一样的高光。无簇信息时退回 part_id+时间区间。"""
    return m.get("_ckey") or (m["part_id"], m["start"], m["end"])


def _overlap_len(a: dict, b: dict) -> float:
    """两段 mark 的时间交集长度（秒）；不同 Part 恒为 0。"""
    if a["part_id"] != b["part_id"]:
        return 0.0
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _cluster_marks(items: list[dict], kind: str) -> None:
    """同 Part 内 IoU>50% 的同类段打上相同 _ckey。items 须已按分数降序——
    簇代表取分数最高的那段，后来的近似段并入其簇。"""
    by_part: dict[int, list[dict]] = {}
    for m in items:
        by_part.setdefault(m["part_id"], []).append(m)
    seq = 0
    for pid, ms in by_part.items():
        reps: list[dict] = []
        for m in ms:
            for r in reps:
                inter = _overlap_len(m, r)
                union = (m["end"] - m["start"]) + (r["end"] - r["start"]) - inter
                if union > 0 and inter / union > 0.5:
                    m["_ckey"] = r["_ckey"]
                    break
            else:
                seq += 1
                m["_ckey"] = (pid, kind, seq)
                reps.append(m)


# 双高光枚举上限：组合数是候选配对数的平方，配对多了接口会卡住。按分数取前 60 个配对
# 参与组合，最多约 1770 种，足够覆盖各档时长；被截掉的部分打日志，不静默丢。
_DOUBLE_POOL_MAX = 60


def _pair_is_legal(mode: str, hl: dict, hk: dict, parts_ordered: list[dict]) -> bool:
    """这一对高光/钩子在该模式下能不能出片。

    高光顺剪和双高光顺剪都是顺剪，钩子必须排在高光之后（同 Part 看时间、跨 Part 看
    part_index）。双高光过去漏了这条判定：钩子取自更早的一集时，白场会先播完高光那集的
    尾巴、再跳回前面某集的开头，画面时间倒着走，而且两集的大段白场都算进了成片长度。
    真实项目「132」上 352 种双高光组合里有 120 种（34%）是这种逆序配对。

    悬疑倒置不走这里，它一组是三个标记，合法性判断见 _xuanyi_triples。"""
    return _estimate_pair_duration(hl, hk, parts_ordered) is not None


def _part_offsets(parts_ordered: list[dict]) -> dict[int, float]:
    """每个 Part 在"整部剧"时间轴上的起点偏移，用于把跨 Part 的两段时间放在一起比较。"""
    offsets: dict[int, float] = {}
    acc = 0.0
    for p in sorted(parts_ordered, key=lambda x: x["part_index"]):
        offsets[p["part_id"]] = acc
        acc += p["part_duration"] or 0.0
    return offsets


def _pair_span(pair: tuple[dict, dict], offsets: dict[int, float]) -> tuple[float, float]:
    """一组配对在成片里实际覆盖的剧内时间跨度：高光起点 → 钩子终点（中间全是白场）。"""
    hl, hk = pair
    return (offsets.get(hl["part_id"], 0.0) + hl["start"],
            offsets.get(hk["part_id"], 0.0) + hk["end"])


def _double_combo_ok(first: tuple[dict, dict], second: tuple[dict, dict],
                     offsets: dict[int, float]) -> bool:
    """双高光的两组配对能不能凑成一条成片。四个条件：

    - 两段高光不能是同一段；
    - 两个钩子也不能是同一段（否则同一截钩子在一条成片里播两遍）。这条对齐改造前逐条
      挑选时 used_hk_keys 的去重效果，别在换成组合枚举后丢掉；
    - 两段高光不能同 Part 重叠超过较短段一半（两段几乎一样的画面拼一起，观众等于连看两遍）；
    - 两组配对的剧内跨度不能相交。每组是"高光 → 白场 → 钩子"一整条连续的时间，跨度相交
      时后一组的画面已经在前一组的白场里播过一遍。真实项目「132」上有 42 种组合命中。
    """
    hl_first, hk_first = first
    hl_second, hk_second = second
    if _mark_key(hl_first) == _mark_key(hl_second):
        return False
    if _mark_key(hk_first) == _mark_key(hk_second):
        return False
    inter = _overlap_len(hl_first, hl_second)
    shorter = min(hl_first["end"] - hl_first["start"], hl_second["end"] - hl_second["start"])
    if inter > 0.5 * shorter:
        return False
    a0, a1 = _pair_span(first, offsets)
    b0, b1 = _pair_span(second, offsets)
    return min(a1, b1) <= max(a0, b0)


# 悬疑倒置的候选是三元组，数量是 高光数 × 钩子数²，比配对多一个量级。
# 超过这个数按开场悬念的分数截断，防止大项目上枚举把导出接口拖住。
_XUANYI_POOL_MAX = 3000


def _xuanyi_triples(highlights: list[dict], hooks: list[dict],
                    parts_ordered: list[dict]) -> list[tuple[dict, dict, dict]]:
    """悬疑倒置的合法组合：(开场悬念, 高光, 收尾钩子)。

    三条约束，都来自「倒叙」这个语义本身：
    - 开场悬念取自最晚的位置——它是后文的悬念，提到片头来才叫倒置；
    - 高光排在开场悬念之前，收尾钩子排在高光之后，这样中间那段剧情是顺着走的；
    - 收尾钩子不能越过开场悬念，否则中间剧情会把片头那段悬念再播一遍
      （双高光的「跨度不能相交」防的是同一件事）。
    """
    offsets = _part_offsets(parts_ordered)
    def at(mark: dict, key: str) -> float:
        return offsets.get(mark["part_id"], 0.0) + mark[key]

    out: list[tuple[dict, dict, dict]] = []
    for opener in hooks:
        op_start = at(opener, "start")
        for hl in highlights:
            if at(hl, "end") >= op_start:
                continue
            for closer in hooks:
                if _mark_key(closer) == _mark_key(opener):
                    continue
                if at(closer, "start") <= at(hl, "end"):
                    continue
                if at(closer, "end") > op_start:
                    continue
                out.append((opener, hl, closer))
    if len(out) > _XUANYI_POOL_MAX:
        out.sort(key=lambda t: -(t[0]["score"] + t[1]["score"] + t[2]["score"]))
        log_event("admake.export.xuanyi_pool_capped",
                  total=len(out), capped_to=_XUANYI_POOL_MAX)
        out = out[:_XUANYI_POOL_MAX]
    return out


def _estimate_plan_duration(mode: str, pairs: list[tuple[dict, dict]],
                            parts_ordered: list[dict]) -> float:
    """这套配对最终会出多长的成片，按 _plan_export 真实产出的段落累加。

    选材的估算口径必须和最终成片同源。2026-08-10 那次 3 分钟目标导出 6 分 12 秒，
    根子就在估算和实际各算一套，所以这里直接调用产出 plan 的那个函数，不另写公式。
    """
    return sum(seg["end"] - seg["start"]
               for seg in _plan_export(mode, pairs, parts_ordered)
               if seg["end"] > seg["start"])


def _rank_combos(mode: str, highlights: list[dict], hooks: list[dict],
                 parts_ordered: list[dict],
                 target_sec: float) -> list[tuple[list[tuple[dict, dict]], float]]:
    """把所有能出片的素材组合按「成片时长离目标多近」排序，贴合度相同时分数高的在前。

    返回 [(配对列表, 成片时长秒), ...]。配对列表是一条成片要用的全部配对：高光顺剪和
    悬疑倒置各 1 对，双高光 2 对。三种模式共用这一个排序口径，界面上的目标时长因此对
    全部模式生效。时长跟着一起返回，_plan_combos 排每条用哪套时还要拿它把关。
    """
    if mode == "xuanyi_daozhi":
        scored = [([t], _estimate_plan_duration(mode, [t], parts_ordered))
                  for t in _xuanyi_triples(highlights, hooks, parts_ordered)]
        scored.sort(key=lambda item: (
            abs(item[1] - target_sec),
            -sum(mark["score"] for group in item[0] for mark in group),
        ))
        return scored

    singles = [(hl, hk) for hl in highlights for hk in hooks
               if _pair_is_legal(mode, hl, hk, parts_ordered)]
    if mode != "shuanggaoguang":
        scored = [([pair], _estimate_plan_duration(mode, [pair], parts_ordered))
                  for pair in singles]
    else:
        offsets = _part_offsets(parts_ordered)
        singles.sort(key=lambda p: -(p[0]["score"] + p[1]["score"]))
        if len(singles) > _DOUBLE_POOL_MAX:
            log_event("admake.export.double_pool_capped",
                      total_pairs=len(singles), capped_to=_DOUBLE_POOL_MAX)
            singles = singles[:_DOUBLE_POOL_MAX]
        scored = []
        for i, first in enumerate(singles):
            for second in singles[i + 1:]:
                if not _double_combo_ok(first, second, offsets):
                    continue
                combo = [first, second]
                scored.append((combo, _estimate_plan_duration(mode, combo, parts_ordered)))
    scored.sort(key=lambda item: (
        abs(item[1] - target_sec),
        -sum(mark["score"] for group in item[0] for mark in group),
    ))
    return scored


# 批量导出里每条都得贴着目标时长走。让更多素材出镜（均衡）只能在时长差不多的组合之间挑，
# 不能拿时长去换：双高光一条成片要用 4 段素材，可达时长又能从 3 分钟横跨到 50 分钟，纯按
# 出镜次数挑，第 2 条起就会为了让没露过面的素材上场跳到极长的组合上。
# 实测（10 集 × 6 分钟、每集 3 高光 1 钩子，双高光，目标 3 分钟，导 5 条）：
#   修前 2分56 / 6分46 / 46分36 / 44分36 / 6分36
#   修后 2分56 / 3分16 / 3分16 / 3分26 / 3分26
_DUR_TOL_RATIO = 0.15   # 允许偏离目标的比例
_DUR_TOL_MIN = 15.0     # 目标很短时的兜底容差（秒）
_DUR_TOL_RELAX = 3      # 贴合档内的组合用完后，最多再放宽几档


def _dur_limit(scored: list[tuple[list[tuple[dict, dict]], float]], target_sec: float) -> float:
    """时长贴合门槛：最贴合那条的偏差再加一档容差。素材均衡只在门槛内挑。"""
    return abs(scored[0][1] - target_sec) + max(target_sec * _DUR_TOL_RATIO, _DUR_TOL_MIN)


def _dur_pool_size(scored: list[tuple[list[tuple[dict, dict]], float]], target_sec: float) -> int:
    """这批导出能做到互不重复的条数上限。

    口径必须和 _plan_combos 的实际行为一致：贴合档用完后它还能放宽 _DUR_TOL_RELAX 档
    继续拿没用过的组合，所以上限按放宽到头的门槛数，只数贴合档会把上限报小——报告里
    「超出部分会复用素材」的警告会在实际不复用时就出现。"""
    if not scored:
        return 0
    tol_step = max(target_sec * _DUR_TOL_RATIO, _DUR_TOL_MIN)
    limit = _dur_limit(scored, target_sec) + _DUR_TOL_RELAX * tol_step
    return sum(1 for _, d in scored if abs(d - target_sec) <= limit)


def _plan_combos(
    scored: list[tuple[list[tuple[dict, dict]], float]],
    count: int,
    target_sec: float,
) -> list[tuple[list[tuple[dict, dict]], float]]:
    """规划 count 条导出各用哪套配对：先按时长贴合度圈定候选，再在候选里做素材均衡。

    scored 已按 _rank_combos 排好序（离目标时长越近越靠前，同样贴合时分数高的在前），
    每一项是 (配对列表, 成片时长)，三种模式共用这一套规划。规则：
    - 候选门槛 = 最贴合那条的偏差 + 容差。素材再怎么均衡也不许跨出这个门槛；
    - 门槛内选累计出镜最少的素材，权重相同时保持排序顺序（更贴合目标、分数更高的先出）；
    - 门槛内的组合全用过了就放宽门槛（最多 _DUR_TOL_RELAX 档），仍然没有才清空重来
      按均衡轮转，不像旧实现那样从头复制导致连续重复。

    不重复上限 = 放宽到头的门槛内的组合数（_dur_pool_size 按同一口径计算）。
    """
    if not scored:
        return []
    base_limit = _dur_limit(scored, target_sec)
    tol_step = max(target_sec * _DUR_TOL_RATIO, _DUR_TOL_MIN)
    planned: list[tuple[list[tuple[dict, dict]], float]] = []
    used: set[tuple] = set()
    use_count: dict[tuple, int] = {}
    for _ in range(count):
        pool: list[tuple[list[tuple[dict, dict]], float]] = []
        limit = base_limit
        for _ in range(_DUR_TOL_RELAX + 1):   # 第一轮是贴合档本身，之后每轮放宽一档
            pool = [item for item in scored
                    if abs(item[1] - target_sec) <= limit and _combo_key(item[0]) not in used]
            if pool:
                break
            limit += tol_step
        if not pool:          # 组合用尽，重置后回到最贴合的那一档继续均衡轮转
            used.clear()
            pool = [item for item in scored if abs(item[1] - target_sec) <= base_limit] or list(scored)
        # 先比出镜次数，平手再比分数高的。分数只能当平手裁判，不能当主键——
        # 实测真实标记的分数几乎只落在 8 和 9 两档（提示词要 1~10，模型只用中间两档），
        # 拿它当主键排序，平均分只涨 0.05，成片离目标时长却要多偏 20 秒，不划算。
        # 而两档分布让平手频繁发生，这里正是分数唯一派得上用场的位置。
        chosen = min(pool, key=lambda item: (
            sum(use_count.get(_mark_key(mark), 0) for group in item[0] for mark in group),
            -sum(mark["score"] for group in item[0] for mark in group),
        ))
        used.add(_combo_key(chosen[0]))
        for group in chosen[0]:
            for mark in group:
                use_count[_mark_key(mark)] = use_count.get(_mark_key(mark), 0) + 1
        planned.append(chosen)
    return planned


def _combo_key(combo: list[tuple]) -> tuple:
    """一套配对的去重标识：按顺序取每组里每个标记的标识。

    不写死二元：悬疑倒置一组是三个标记（开场钩子、高光、收尾钩子），
    顺剪类仍是两个。这里按实际长度取，两种形状共用。"""
    return tuple(tuple(_mark_key(m) for m in group) for group in combo)


def _no_combo_reason(mode: str, n_hk: int) -> str:
    """凑不出任何合法组合时给用户的报错文案，按模式说清缺什么、怎么补。

    以前这种情况会降级成逐段硬拼，产出违反模式定义的成片（双高光只有 1 个钩子时，
    同一段钩子在一条成片里播两遍；顺剪配不出先后关系时画面时间倒着走）。烂片流进
    投放的代价比导出失败大，所以改成报错并提示补标记。"""
    if mode == "shuanggaoguang":
        if n_hk < 2:
            return "双高光顺剪需要至少 2 个不同的钩子标记，当前不足，请补充钩子后再导出"
        return "现有标记凑不出双高光组合：需要两组「高光在前、钩子在后」且画面互不重叠的配对，请调整标记位置"
    if mode == "xuanyi_daozhi":
        if n_hk < 2:
            return "悬疑倒置需要至少 2 个不同的钩子标记：一个放片头做悬念，一个收尾，当前不足"
        return ("现有标记凑不出悬疑倒置组合：片头那个悬念要取自最靠后的剧情，"
                "高光和收尾钩子都得排在它前面，请调整标记位置")
    if mode == "xuanyi_daozhi":
        return "现有标记凑不出悬疑倒置组合：同一 Part 里钩子要在高光结束之后，请调整标记位置"
    return "现有标记凑不出高光顺剪组合：钩子要在高光之后（跨 Part 按 Part 顺序），请调整标记位置"


def _sanitize_naming_field(value: str, max_len: int, fallback: str) -> str:
    """导出文件名兜底清洗：保留剧名原貌（撇号/逗号/空格等运营习惯用字照留），
    只把文件系统真正非法的字符和字段分隔符（_NAMING_BANNED_RE：/ \\ * ? " < > | _）换成
    连字符、去首尾冒号——强制改写剧名会造成命名歧义，故不做白名单收窄。"""
    cleaned = _NAMING_BANNED_RE.sub('-', value or '').strip(':：')[:max_len]
    return cleaned or fallback


def _build_export_filename(title: str, editor_alias: str, seq: int) -> str:
    """{项目标题}_SkyArt_{M.D}_{导出文件命名}_{编号}，编号由调用方保证项目内唯一。"""
    now = datetime.now()
    clean_title = _sanitize_naming_field(title, _TITLE_MAX_LEN, "未命名项目")
    clean_alias = _sanitize_naming_field(editor_alias, 20, "未署名")
    return f"{clean_title}_SkyArt_{now.month}.{now.day}_{clean_alias}_{seq}"


def _write_export_log(
    log_path: str,
    video_name: str,
    plan: list[dict],
    actual_dur: float,
    target_dur: float,
    mode: str,
    submitted_at: str | None = None,
    batch_info: dict | None = None,
    sticker_dur: float = 0.0,
):
    """
    sticker_dur：模板前后贴片总时长。plan 里只有正片段落，贴片是成片阶段才接上去的，
    不单独列出来的话"片段详情合计"和"总时长"会差出贴片那一段，读报告的人无从解释。

    batch_info（可选，缺失时对应行不写）：
      {"index": 本条在批次中的序号(从1开始), "count": 批次总数,
       "n_hl": 候选高光数, "n_hk": 候选钩子数, "n_combos": 合法组合数(三种模式都有),
       "n_pool": 时长门槛（放宽到头）内的组合数，也就是互不重复的条数上限,
       "part_names": 本次导出范围内的 Part 名称列表}
    """
    def fmt(s: float) -> str:
        m, sec = divmod(s, 60)
        return f"{int(m):02d}:{sec:05.2f}"

    mode_label = {"highlight_shunjian": "高光顺剪", "xuanyi_daozhi": "悬疑倒置",
                  "shuanggaoguang": "双高光顺剪"}.get(mode, mode)
    seg_label  = {"highlight": "高光", "whitespace": "白场", "hook": "钩子"}
    completed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        "=" * 44,
        "SkyArt 广告素材导出报告",
        "=" * 44,
        f"导出文件：{video_name}",
        f"提交时间：{submitted_at or completed_at}",
        f"完成时间：{completed_at}",
        f"导出模式：{mode_label}",
    ]
    if batch_info:
        lines.append(f"批次序号：{batch_info['index']}/{batch_info['count']}")
        pool_line = f"候选池：高光 {batch_info['n_hl']} 个 / 钩子 {batch_info['n_hk']} 个"
        if batch_info.get("n_combos") is not None:
            pool_line += f" / 可用组合 {batch_info['n_combos']} 个"
        if batch_info.get("n_pool"):
            # 本批只在这些组合里轮换：其余组合的时长离目标太远，让素材多露脸也不值得换。
            pool_line += f"（本批可轮换 {batch_info['n_pool']} 个）"
        lines.append(pool_line)
        # 每条成片都要贴着目标时长走，所以能互不重复的只有时长门槛内那部分组合。
        pool_size = batch_info.get("n_pool") or batch_info.get("n_combos") \
            or min(batch_info['n_hl'], batch_info['n_hk'])
        if batch_info['count'] > pool_size:
            lines.append(f"⚠ 请求 {batch_info['count']} 条超过不重复上限 {pool_size} 条，超出部分会复用素材")
        if batch_info.get("part_names"):
            lines.append(f"导出范围：{', '.join(batch_info['part_names'])}")
    # 目标时长是排序的靠拢点，不是硬约束——成片长度由高光起点到钩子终点的跨度决定，
    # 系统只能在现有素材里挑最接近的那个。所以这里报差值，不判达标。
    delta = actual_dur - target_dur
    lines += [
        f"目标时长：{fmt(target_dur)}" + ("（成片总长，含模板贴片）" if sticker_dur > 0 else ""),
        f"实际时长：{fmt(actual_dur)}（{'长' if delta >= 0 else '短'} {fmt(abs(delta))}）",
    ]
    if sticker_dur > 0:
        lines.append(f"  其中模板贴片 {fmt(sticker_dur)}，正片 {fmt(max(0.0, actual_dur - sticker_dur))}")

    lines += ["", "-" * 44, "片段详情", "-" * 44]
    for seg in plan:
        if seg["end"] <= seg["start"]:
            continue
        dur = seg["end"] - seg["start"]
        label_cn = seg_label.get(seg["label"], seg["label"])
        detail = [
            f"[{label_cn}]",
            f"  来源视频：{seg['part_name']}",
            f"  开始时间：{fmt(seg['start'])}",
            f"  结束时间：{fmt(seg['end'])}",
            f"  片段时长：{fmt(dur)}",
        ]
        if seg.get("score") is not None:
            detail.append(f"  评分：{seg['score']}")
        lines += detail + [""]
    if sticker_dur > 0:
        lines += ["[模板贴片]", f"  片头+片尾合计：{fmt(sticker_dur)}", ""]
    lines += ["-" * 44, f"总时长：{fmt(actual_dur)}", "-" * 44]

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── Background tasks ──────────────────────────────────────────────────────────

async def _build_part(part_id: int, source_paths: list[str],
                      limits: list[float | None] | None = None):
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(AdmakePart, AdmakeProject)
            .join(AdmakeProject, AdmakePart.project_id == AdmakeProject.id)
            .where(AdmakePart.id == part_id)
        )
        row = res.first()
        if not row:
            return
        part, project = row
        proj_id, proj_title, part_index = project.id, project.title, part.part_index
        try:
            part.status = "processing"
            await db.commit()
            out_name = f"Part{part_index + 1}.mp4"
            out_path = os.path.join(_project_parts_dir(proj_id, proj_title), out_name)
            abs_paths = [
                p if os.path.isabs(p) else os.path.join(settings.UPLOAD_DIR, p)
                for p in source_paths
            ]
            merged = await vid_svc.merge_videos(abs_paths, out_path, limits)
            dur = await vid_svc.get_duration(merged)
            rel = os.path.relpath(merged, settings.UPLOAD_DIR)
            async with AsyncSessionLocal() as db2:
                res2 = await db2.execute(select(AdmakePart).where(AdmakePart.id == part_id))
                p2 = res2.scalar_one_or_none()
                if p2:
                    p2.file_path = rel; p2.duration_sec = dur; p2.status = "ready"
                    await db2.commit()
            log_event("admake.build_part", part_id=part_id, status="ready", duration_sec=round(dur, 1))
        except Exception as e:
            log_failure("admake.build_part", e, part_id=part_id)
            async with AsyncSessionLocal() as db3:
                res3 = await db3.execute(select(AdmakePart).where(AdmakePart.id == part_id))
                p3 = res3.scalar_one_or_none()
                if p3:
                    p3.status = "failed"; await db3.commit()


async def _cut_part_segment(part_id: int, source_path: str, start_sec: float, end_sec: float):
    """Cut a time slice from a single long source and assign it to the given Part."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(AdmakePart, AdmakeProject)
            .join(AdmakeProject, AdmakePart.project_id == AdmakeProject.id)
            .where(AdmakePart.id == part_id)
        )
        row = res.first()
        if not row:
            return
        part, project = row
        proj_id, proj_title, part_index = project.id, project.title, part.part_index

    out_name = f"Part{part_index + 1}.mp4"
    out_path = os.path.join(_project_parts_dir(proj_id, proj_title), out_name)
    try:
        abs_src = source_path if os.path.isabs(source_path) else os.path.join(settings.UPLOAD_DIR, source_path)
        await vid_svc.cut_video_copy(abs_src, start_sec, end_sec, out_path, normalize=True)
        dur = await vid_svc.get_duration(out_path)
        rel = os.path.relpath(out_path, settings.UPLOAD_DIR)
        async with AsyncSessionLocal() as db2:
            res2 = await db2.execute(select(AdmakePart).where(AdmakePart.id == part_id))
            p2 = res2.scalar_one_or_none()
            if p2:
                p2.file_path = rel; p2.duration_sec = dur; p2.status = "ready"
                await db2.commit()
        log_event("admake.cut_part", part_id=part_id, status="ready", duration_sec=round(dur, 1))
    except Exception as e:
        log_failure("admake.cut_part", e, part_id=part_id)
        async with AsyncSessionLocal() as db3:
            res3 = await db3.execute(select(AdmakePart).where(AdmakePart.id == part_id))
            p3 = res3.scalar_one_or_none()
            if p3:
                p3.status = "failed"; await db3.commit()


async def _copy_source_as_part(part_id: int, source_path: str, duration_sec: float,
                               limit: float | None = None):
    """For a short single-source: copy the source into the organized parts dir."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(AdmakePart, AdmakeProject)
            .join(AdmakeProject, AdmakePart.project_id == AdmakeProject.id)
            .where(AdmakePart.id == part_id)
        )
        row = res.first()
        if not row:
            return
        part, project = row
        proj_id, proj_title, part_index = project.id, project.title, part.part_index

    abs_src = source_path if os.path.isabs(source_path) else os.path.join(settings.UPLOAD_DIR, source_path)
    out_name = f"Part{part_index + 1}.mp4"
    out_path = os.path.join(_project_parts_dir(proj_id, proj_title), out_name)
    rel = os.path.relpath(out_path, settings.UPLOAD_DIR)
    try:
        # 此前无 try/except：复制失败或源缺失会让 status 永远停在 "processing"，
        # 前端永久显示"视频合成中"。且源缺失时旧逻辑还会误置 ready（指向不存在的文件）。
        if not os.path.exists(abs_src):
            raise FileNotFoundError(f"源文件不存在: {abs_src}")
        # 单素材也必须归一化：merge_videos 单输入时按目标规格判定——已达标走
        # 秒级 copy，未达标（如 1080x1080 方形源）重编码到 720x1280@30。
        # 直接 shutil.copy2 会让 Part 保留源规格，与其他 Part 混规格，导出必坏。
        await vid_svc.merge_videos([abs_src], out_path, [limit])
        dur = await vid_svc.get_duration(out_path)
        async with AsyncSessionLocal() as db2:
            res2 = await db2.execute(select(AdmakePart).where(AdmakePart.id == part_id))
            p2 = res2.scalar_one_or_none()
            if p2:
                p2.file_path = rel; p2.duration_sec = dur; p2.status = "ready"
                await db2.commit()
        log_event("admake.copy_source", part_id=part_id, status="ready", duration_sec=round(dur, 1))
    except Exception as e:
        log_failure("admake.copy_source", e, part_id=part_id)
        async with AsyncSessionLocal() as db3:
            res3 = await db3.execute(select(AdmakePart).where(AdmakePart.id == part_id))
            p3 = res3.scalar_one_or_none()
            if p3:
                p3.status = "failed"; await db3.commit()


async def _apply_template_if_any(final_path: str, template_id: int | None, project_id: int) -> tuple[str, str | None]:
    """成片产出后套用成片模板（前后贴片 / 文字 / 贴纸 / 特效 / 闪白），原地替换 final_path。
    模板查不到（如默认模板已删——SQLite 不强制外键会留悬空 id）或套用报错时，
    保留未套用的成片、不阻断导出。返回 (最终文件路径, 模板失败原因或 None)——
    失败原因由调用方写进导出记录，历史里标出来，不能让"没套模板的白片"静默标就绪。"""
    if not template_id:
        return final_path, None
    from app.database import AsyncSessionLocal
    from app.services import postprocess
    tmp_out = final_path[:-4] + "_tpl.mp4"
    t0 = _time.monotonic()
    try:
        async with AsyncSessionLocal() as db:
            t = (await db.execute(select(AdmakeTemplate).where(AdmakeTemplate.id == template_id))).scalar_one_or_none()
            proj = (await db.execute(select(AdmakeProject).where(AdmakeProject.id == project_id))).scalar_one_or_none()
        if not t:
            log_event("admake.template.skip", template_id=template_id, reason="template_not_found")
            return final_path, "所选模板已被删除，本条未套模板"
        # 归属校验：模板必须属于项目所有者（其余模板路由都有 _get_owned_template，这里补齐）
        if proj and t.user_id != proj.user_id:
            log_event("admake.template.skip", template_id=template_id, reason="not_owned")
            return final_path, "所选模板不属于当前账号，本条未套模板"
        config = json.loads(t.config or "{}")
        if not (config.get("intro") or config.get("outro") or config.get("texts")
                or config.get("pngs") or config.get("dyns")):
            return final_path, None  # 空模板，无需套用
        fields = ({"title": proj.title, "editor_alias": proj.editor_alias,
                   "target_platform": proj.target_platform} if proj else {})
        # 绝对路径：素材路径要交给 video.get_video_info，相对路径会被再拼一次
        # UPLOAD_DIR，变成 uploads/uploads/... 后 ffprobe 失败
        asset_dir = os.path.abspath(
            os.path.join(settings.UPLOAD_DIR, "AdMake", "Templates", f"template_{template_id}"))
        # 先查素材还在不在。缺文件时 ffmpeg 会以 RuntimeError 冒出来，落到下面的兜底文案里
        # 就只剩一个异常类型名，运营看到「模板套用失败（RuntimeError）」不知道发生了什么。
        # 素材目录被清掉（换机器、手动清空间、导入的包没带素材）是最常见的一种，单独报出来。
        from app.routers.admake_template import _config_assets
        missing = sorted(a for a in _config_assets(config)
                         if not os.path.isfile(os.path.join(asset_dir, a)))
        if missing:
            log_event("admake.template.skip", template_id=template_id,
                      reason="assets_missing", missing=len(missing))
            return final_path, (f"模板素材文件缺失（{missing[0]} 等 {len(missing)} 个），"
                                f"本条未套模板，请重新上传素材")
        await postprocess.apply_template(final_path, config, fields, asset_dir, tmp_out)
        os.replace(tmp_out, final_path)
        log_event("admake.template.applied", template_id=template_id,
                  export_file=os.path.basename(final_path),
                  elapsed_ms=int((_time.monotonic() - t0) * 1000))
        return final_path, None
    except Exception as e:
        log_failure("admake.template", e, template_id=template_id,
                    elapsed_ms=int((_time.monotonic() - t0) * 1000))
        # 半成品不能留在用户导出目录里——名字像正片，会被错拿去投放
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        return final_path, f"模板套用失败（{type(e).__name__}），本条未套模板"  # 成片保留，导出不失败


async def _run_export(export_id: int, part_file_path: str, segments: list[tuple[float, float]], project_id: int, project_title: str, template_id: int | None = None, export_path: str | None = None):
    from app.database import AsyncSessionLocal
    abs_part = part_file_path if os.path.isabs(part_file_path) else os.path.join(settings.UPLOAD_DIR, part_file_path)
    exports_dir = _project_exports_dir(project_id, project_title, export_path)
    clip_paths: list[str] = []
    out_path = ""  # 失败清理要用；裁剪阶段就挂时它还没被赋值
    try:
        for i, (start, end) in enumerate(segments):
            if end <= start:
                continue
            clip_out = os.path.join(exports_dir, f"_clip_{export_id}_{i}_{uuid.uuid4().hex[:6]}.mp4")
            await vid_svc.cut_video_copy(abs_part, start, end, clip_out, normalize=True)
            clip_paths.append(clip_out)

        if not clip_paths:
            raise ValueError("No valid segments")

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        out_name = f"export_{ts}_{export_id}.mp4"
        out_path = os.path.join(exports_dir, out_name)
        final = await vid_svc.export_final_video(clip_paths, out_path)
        final, tpl_err = await _apply_template_if_any(final, template_id, project_id)
        dur = await vid_svc.get_duration(final)
        rel = _export_rel(final)

        async with AsyncSessionLocal() as db:
            res = await db.execute(select(AdmakeExport).where(AdmakeExport.id == export_id))
            ex = res.scalar_one_or_none()
            if ex:
                ex.status = "ready"; ex.file_path = rel; ex.duration_sec = dur
                ex.template_error = tpl_err
                await db.commit()
        log_event("admake.export", export_id=export_id, status="ready", duration_sec=round(dur, 1),
                  template_id=template_id, template_error=tpl_err,
                  custom_dir=bool(export_path), file=os.path.basename(final))
    except Exception as e:
        log_failure("admake.export", e, export_id=export_id)
        # 失败的半成品不能留在导出目录——名字像正片，会被错拿去投放
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except Exception:
            pass
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(AdmakeExport).where(AdmakeExport.id == export_id))
            ex = res.scalar_one_or_none()
            if ex:
                ex.status = "failed"; await db.commit()
    finally:
        for p in clip_paths:
            try: os.remove(p)
            except Exception: pass


def _clip_tmp_dir() -> str:
    """裁切片的落脚处。放在应用自己的目录里，不放用户的导出目录。

    导出中途被打死时（Electron 退出会把后端 os._exit 掉），清理代码不执行，
    切片就留在原地。写在导出目录里，用户打开交付文件夹看到的是一堆几十兆的
    _clip_*.mp4 混在成片中间。
    """
    d = os.path.join(settings.UPLOAD_DIR, "AdMake", "_export_tmp")
    os.makedirs(d, exist_ok=True)
    return d


async def _run_export_multi(
    export_id: int,
    plan: list[dict],
    project_id: int,
    project_title: str,
    target_dur: float,
    mode: str,
    filename_stem: str,
    batch_dir: str | None = None,
    submitted_at: str | None = None,
    batch_info: dict | None = None,
    template_id: int | None = None,
    sticker_dur: float = 0.0,
):
    """跨 Part 导出：根据 plan 裁剪并合并各段，同时生成 log 文件。"""
    from app.database import AsyncSessionLocal
    out_dir = batch_dir if batch_dir else _project_exports_dir(project_id, project_title)

    MAX_ATTEMPTS = 2  # 首次 + 重试一次
    last_err: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        clip_paths: list[str] = []
        try:
            for i, seg in enumerate(plan):
                if seg["end"] <= seg["start"]:
                    continue
                abs_src = (seg["file_path"] if os.path.isabs(seg["file_path"])
                           else os.path.join(settings.UPLOAD_DIR, seg["file_path"]))
                clip_out = os.path.join(_clip_tmp_dir(), f"_clip_{export_id}_{i}_{uuid.uuid4().hex[:6]}.mp4")
                await vid_svc.cut_video_copy(abs_src, seg["start"], seg["end"], clip_out, normalize=True)
                clip_paths.append(clip_out)

            if not clip_paths:
                raise ValueError("No valid segments")

            out_name = f"{filename_stem}.mp4"
            out_path = os.path.join(out_dir, out_name)
            final = await vid_svc.export_final_video(clip_paths, out_path)
            final, tpl_err = await _apply_template_if_any(final, template_id, project_id)
            actual_dur = await vid_svc.get_duration(final)
            rel = _export_rel(final)

            log_path = out_path[:-4] + ".log.txt"
            # 诊断报告写失败绝不能牵连成片：成片这时已经编完并套好模板了，
            # 而外层 except 会先把它删掉再重试，两次都失败就整条置成 failed。
            # 磁盘满、导出目录被改名都会走到这里。与 AI 分析那条链路的 _wlog 同一道防护。
            try:
                _write_export_log(log_path, out_name, plan, actual_dur, target_dur, mode,
                                   submitted_at=submitted_at, batch_info=batch_info,
                                   sticker_dur=sticker_dur)
            except Exception as _e:
                log_failure("admake.export_multi.log", _e, export_id=export_id)

            async with AsyncSessionLocal() as db:
                res = await db.execute(select(AdmakeExport).where(AdmakeExport.id == export_id))
                ex = res.scalar_one_or_none()
                if ex:
                    ex.status = "ready"; ex.file_path = rel; ex.duration_sec = actual_dur
                    ex.template_error = tpl_err
                    await db.commit()
            # mode 与 target_duration 必须落在这条里：成片长度不符时要能一眼看出用户选的是
            # 哪个模式、目标多少，不然只能去翻数据库（业务同学的机器上翻不到）。
            log_event("admake.export_multi", export_id=export_id, status="ready", duration_sec=round(actual_dur, 1),
                      mode=mode, target_duration=round(target_dur, 1),
                      sticker_sec=round(sticker_dur, 2),
                      body_sec=round(max(0.0, actual_dur - sticker_dur), 1),
                      delta_sec=round(actual_dur - target_dur, 1),
                      template_id=template_id, template_error=tpl_err,
                      custom_dir=bool(batch_dir and not batch_dir.startswith(settings.UPLOAD_DIR)),
                      file=out_name, attempt=attempt + 1)
            for p in clip_paths:
                try: os.remove(p)
                except Exception: pass
            return  # 成功，退出

        except Exception as e:
            last_err = e
            for p in clip_paths:
                try: os.remove(p)
                except Exception: pass
            # 半成品成片不能留在导出目录——名字像正片，会被错拿去投放（重试会重写，最终失败必须删）
            try:
                _half = os.path.join(out_dir, f"{filename_stem}.mp4")
                if os.path.isfile(_half):
                    os.remove(_half)
            except Exception:
                pass
            if attempt < MAX_ATTEMPTS - 1:
                log_failure("admake.export_multi.retry", e, export_id=export_id, attempt=attempt + 1)
            else:
                log_failure("admake.export_multi", e, export_id=export_id, attempts=MAX_ATTEMPTS)
                async with AsyncSessionLocal() as db:
                    res = await db.execute(select(AdmakeExport).where(AdmakeExport.id == export_id))
                    ex = res.scalar_one_or_none()
                    if ex:
                        ex.status = "failed"; await db.commit()


# ── AI Analysis (2.0.0.10) ───────────────────────────────────────────────────

# In-memory task state: { project_id: { status, progress, total, error } }
_ai_tasks: dict[int, dict] = {}

def _load_prompt() -> str:
    import sys as _sys
    if getattr(_sys, "frozen", False):
        # PyInstaller onedir: 数据文件在 sys._MEIPASS (_internal/) 下
        p = pathlib.Path(_sys._MEIPASS) / "prompts" / "SkyArt_Prompt.md"
    else:
        # 开发模式: 从 backend/app/routers/ 向上 4 级到项目根目录
        p = pathlib.Path(__file__).parent.parent.parent.parent / "prompts" / "SkyArt_Prompt.md"
    return p.read_text(encoding="utf-8")

AI_ANALYSIS_PROMPT: str = _load_prompt()


# 视频剪辑（AdMake）侧指定的通义模型。白名单通道 2026-08-26 复扫确认已授权，
# 敏感剧情梗概分析不被拦截；视觉输入走 OpenAI 兼容端点即可（DE/key.md 实测）。
# 2026-08-29 在白名单通道上用真实短剧素材横评三个可用模型（同一段 90 秒素材、
# 同一条提示词）：qwen3-vl-plus 13.2 秒出 13 段高光，每段 2~10 秒；qwen3.7-plus
# 27.4 秒只出 3 段，每段 15~37 秒，等于把整集切成三大块，剪辑还得自己找点；
# qwen3.8-max 92.1 秒、输出 4562 token，结果与 3.7-plus 相当。vl-plus 是唯一
# 专做视觉理解的一档，快一倍、便宜一半、颗粒度可用，选它。
# 通道内还能调的：qwen3.7-plus、qwen3.7-flash、qwen3.8-max。其余（含 qwen3-vl-flash、
# qwen3.8-flash、各种 -us 后缀变体）实测全报 access_denied——换模型前先确认它在
# 这条通道的授权名单里，否则不是代码问题。
QWEN_VL_MODEL = "qwen3-vl-plus"


async def _analyze_one_part(part: AdmakePart, on_stage=None) -> dict:
    """Compress video → upload to US OSS → call Qwen via CN DashScope compatible-mode.
    Returns {"parsed": dict, "raw": str, "orig_bytes": int, "comp_bytes": int}.
    on_stage: 可选回调，在 compressing/uploading/analyzing 三个阶段切换时上报（进度展示用）。

    链路取舍（2026-07-10 评测验证，历史坑见 KNOWLEDGE #15）：
    - key/端点必须用 QWEN_API_KEY + QWEN_BASE_URL（阿里云白名单通道，美国 us-east-1）：HAPPYHORSE key 对
      chat 接口无任何模型授权，用它这条分支从未跑通过；
    - 视频经 OSS 签名 URL + video_url 传入：国内端点不接受 Files API 的
      video: file_id 传法（要求 list）；美区 OSS 签名 URL 国内端点可正常拉取。"""
    api_key = settings.QWEN_API_KEY
    if not api_key:
        raise RuntimeError("未配置 QWEN_API_KEY，请在设置中填写通义 API Key 后重启应用")
    base = settings.QWEN_BASE_URL.rstrip("/")
    compat_base = re.sub(r"/api/v1$", "/compatible-mode/v1", base)

    abs_src = os.path.join(settings.UPLOAD_DIR, part.file_path) if not os.path.isabs(part.file_path) else part.file_path
    compressed = os.path.join(_ai_tmp_dir(), f"ai_tmp_{part.id}.mp4")
    orig_bytes = os.path.getsize(abs_src)
    comp_bytes = 0
    oss_key = None

    try:
        # 1. Compress video
        if on_stage:
            on_stage("compressing")
        _, comp_skip_bytes = await vid_svc.compress_for_ai(abs_src, compressed)
        comp_bytes = os.path.getsize(compressed)

        # 2. Upload to US OSS（与 gemini 分支同源）
        if on_stage:
            on_stage("uploading")
        oss_url, oss_key = await _upload_to_oss_us(compressed)

        # 3. Call Qwen for analysis
        if on_stage:
            on_stage("analyzing")
        payload = {
            "model": QWEN_VL_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": oss_url}},
                    {"type": "text", "text": AI_ANALYSIS_PROMPT},
                ],
            }],
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            ai_resp = await client.post(
                f"{compat_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-DashScope-DataInspection": '{"input":"disable","output":"disable"}',
                },
                json=payload,
            )
        if not ai_resp.is_success:
            raise RuntimeError(f"AI 分析失败 {ai_resp.status_code}: {ai_resp.text[:300]}")

        raw_content = ai_resp.json()["choices"][0]["message"]["content"]
        if isinstance(raw_content, list):
            raw_content = "".join(c.get("text", "") for c in raw_content if isinstance(c, dict))
        json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if not json_match:
            raise RuntimeError(f"AI 响应中未找到 JSON: {raw_content[:200]}")
        return {
            "parsed": json.loads(json_match.group()),
            "raw": raw_content,
            "orig_bytes": orig_bytes,
            "comp_bytes": comp_bytes,
            "comp_skip_bytes": comp_skip_bytes,
        }

    finally:
        try:
            os.remove(compressed)
        except Exception:
            pass
        if oss_key:
            await _delete_oss_us(oss_key)


def _parse_ts(val) -> float:
    """Parse AI timestamp: accepts 'mm:ss.cc' string or plain float/int seconds.

    结果取整到秒。Gemini 默认按 1 帧/秒采样视频、时间戳也按秒给（要亚秒精度必须显式
    提高采样率），模型吐出来的小数位没有对应的画面依据。前端的显示和拖拽吸附同样到秒，
    三处口径一致，避免"界面显示 33 秒、成片却从 33.5 秒开始剪"。
    """
    if isinstance(val, (int, float)):
        return float(round(val))
    s = str(val).strip()
    m = re.match(r'^(\d+):(\d{2})\.(\d{2})$', s)
    if m:
        return float(round(int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 100))
    return float(round(float(s)))


async def _save_ai_marks(part_id: int, highlights: list, hooks: list, max_dur: float):
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(delete(AdmakeMark).where(AdmakeMark.part_id == part_id))
        for gi, hl in enumerate(highlights):
            s = max(0.0, min(_parse_ts(hl.get("start", 0)), max_dur))
            e = max(0.0, min(_parse_ts(hl.get("end", 0)), max_dur))
            score = hl.get("score")
            if e > s:
                db.add(AdmakeMark(part_id=part_id, mark_type="highlight_start", group_index=gi, time_sec=s, score=score))
                db.add(AdmakeMark(part_id=part_id, mark_type="highlight_end",   group_index=gi, time_sec=e, score=score))
        for gi, hk in enumerate(hooks):
            s = max(0.0, min(_parse_ts(hk.get("start", 0)), max_dur))
            e = max(0.0, min(_parse_ts(hk.get("end", 0)), max_dur))
            score = hk.get("score")
            if e > s:
                db.add(AdmakeMark(part_id=part_id, mark_type="hook_start", group_index=gi, time_sec=s, score=score))
                db.add(AdmakeMark(part_id=part_id, mark_type="hook_end",   group_index=gi, time_sec=e, score=score))
        await db.commit()


def _get_oss_us_bucket():
    """返回美区 OSS Bucket 对象，用于 Gemini 代理上传。"""
    import oss2
    if not (settings.OSS_KEY_SkyArt_US and settings.OSS_SECRET_SkyArt_US
            and settings.OSS_BUCKET_SkyArt_US):
        raise RuntimeError(
            "美区 OSS 未配置，请在 .env 中填写 OSS_KEY_SkyArt_US / "
            "OSS_SECRET_SkyArt_US / OSS_BUCKET_SkyArt_US"
        )
    auth = oss2.Auth(settings.OSS_KEY_SkyArt_US, settings.OSS_SECRET_SkyArt_US)
    return oss2.Bucket(auth, settings.OSS_ENDPOINT_SkyArt_US, settings.OSS_BUCKET_SkyArt_US)


async def _upload_to_oss_us(abs_file_path: str) -> tuple[str, str]:
    """上传文件到美区 OSS，返回 (signed_url, oss_key)。有效期 1 小时。"""
    if not os.path.exists(abs_file_path):
        raise FileNotFoundError(f"本地文件不存在: {abs_file_path}")

    filename = os.path.basename(abs_file_path)
    oss_key = f"skyart-gemini/{int(_time.time())}_{uuid.uuid4().hex[:8]}_{filename}"

    def _do_upload():
        bucket = _get_oss_us_bucket()
        with open(abs_file_path, "rb") as f:
            bucket.put_object(oss_key, f, headers={"Content-Type": "video/mp4"})
        return bucket.sign_url("GET", oss_key, 3600, slash_safe=True)

    loop = asyncio.get_running_loop()
    signed_url = await loop.run_in_executor(None, _do_upload)
    return signed_url, oss_key


async def _delete_oss_us(oss_key: str):
    """异步删除美区 OSS 上的临时文件。"""
    def _do_delete():
        try:
            _get_oss_us_bucket().delete_object(oss_key)
        except Exception:
            pass
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _do_delete)


async def _precheck_ai_credentials(ai_model: str) -> None:
    """AI 分析入口的密钥快速体检：缺配置/密钥无效必须在这里秒级报错。
    真正用到密钥的动作（OSS 上传、调大模型）发生在每个 Part 压缩完之后，
    叠加重试和多 Part 串行，缺密钥要等约 10 分钟才暴露（2026-07-02 事故）。
    OSS 用只读免费的 get_bucket_info 实测连通性；DashScope 用 /models 只读端点；
    Gemini 代理没有健康检查端点，只能做非空检查。"""
    # gemini / 通义两条链路的视频都经美区 OSS 中转，OSS 凭证实测对两者都必做。
    async def _probe_oss_us():
        missing = [name for name, value in (
            ("OSS_KEY_SkyArt_US", settings.OSS_KEY_SkyArt_US),
            ("OSS_SECRET_SkyArt_US", settings.OSS_SECRET_SkyArt_US),
            ("OSS_BUCKET_SkyArt_US", settings.OSS_BUCKET_SkyArt_US),
        ) if not value]
        if missing:
            raise HTTPException(400, f"美区 OSS 未配置（{'、'.join(missing)}），请在设置中补全后重启应用")

        def _probe():
            import oss2
            auth = oss2.Auth(settings.OSS_KEY_SkyArt_US, settings.OSS_SECRET_SkyArt_US)
            bucket = oss2.Bucket(
                auth, settings.OSS_ENDPOINT_SkyArt_US, settings.OSS_BUCKET_SkyArt_US,
                connect_timeout=OSS_PRECHECK_TIMEOUT_SEC,
            )
            bucket.get_bucket_info()

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _probe), timeout=OSS_PRECHECK_TIMEOUT_SEC + 5
            )
        except asyncio.TimeoutError:
            raise HTTPException(400, "美区 OSS 连接超时，请检查网络后重试")
        except Exception as e:
            error_code = getattr(e, "code", "")
            if error_code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
                raise HTTPException(400, "美区 OSS 密钥无效（AccessKey/Secret 错误），请检查设置后重启应用")
            if error_code == "NoSuchBucket":
                raise HTTPException(400, f"美区 OSS Bucket 不存在：{settings.OSS_BUCKET_SkyArt_US}，请检查设置")
            if error_code == "AccessDenied":
                return  # 密钥有效但账号无查询 bucket 信息的权限，上传权限无法在此验证，放行
            log_failure("admake.precheck_oss_us", e)
            raise HTTPException(400, f"美区 OSS 连接失败：{str(e)[:120]}")

    if ai_model.startswith("gemini"):
        if not settings.GEMINI_PROXY_URL:
            raise HTTPException(400, "未配置 GEMINI_PROXY_URL，请在设置中填写 Dscript 代理接口地址")
        if not settings.GEMINI_PROXY_KEY:
            raise HTTPException(400, "未配置 GEMINI_PROXY_KEY，请在设置中填写 Dscript 代理密钥后重启应用")
        await _probe_oss_us()
    else:
        # 通义分支：QWEN_API_KEY + 国内端点（不是 HAPPYHORSE 美区 key，见 KNOWLEDGE #15）。
        if not settings.QWEN_API_KEY:
            raise HTTPException(400, "未配置 QWEN_API_KEY，请在设置中填写通义 API Key 后重启应用")
        await _probe_oss_us()
        base = settings.QWEN_BASE_URL.rstrip("/")
        compat_base = re.sub(r"/api/v1$", "/compatible-mode/v1", base)
        try:
            async with httpx.AsyncClient(timeout=HAPPYHORSE_PRECHECK_TIMEOUT_SEC) as client:
                # identity：曾出现响应头声明 gzip 但响应体非 gzip 导致 zlib 解压失败
                # （KNOWLEDGE #16，仅安装版 App 进程复现）；此响应仅 7KB，禁用压缩
                # 直接消除解压环节。异常时记录原始首字节以便下次取证。
                resp = await client.get(
                    f"{compat_base}/models",
                    headers={
                        "Authorization": f"Bearer {settings.QWEN_API_KEY}",
                        "Accept-Encoding": "identity",
                        # 白名单通道每个请求都要关绿网，漏了这里会拿到 access_denied，
                        # 预检把它当成 key 无效上报，用户换多少次 key 都过不了
                        "X-DashScope-DataInspection": '{"input":"disable","output":"disable"}',
                    },
                )
        except httpx.DecodingError as e:
            diag_headers = dict(getattr(getattr(e, "response", None), "headers", {}) or {})
            log_failure("admake.precheck_qwen", e, response_headers=str(diag_headers)[:300])
            raise HTTPException(400, f"通义服务响应异常（内容编码损坏，多与代理/网络中间层有关）：{str(e)[:100]}")
        except Exception as e:
            log_failure("admake.precheck_qwen", e)
            raise HTTPException(400, f"通义服务连接失败，请检查网络：{str(e)[:120]}")
        if resp.status_code in (401, 403):
            raise HTTPException(400, "QWEN_API_KEY 无效或已过期，请检查设置后重启应用")
        if resp.status_code >= 500:
            raise HTTPException(400, f"通义服务异常（HTTP {resp.status_code}），请稍后重试")


def _proxy_err_msg(status_code: int, body: str) -> str:
    """把代理错误整理成干净失败原因：网关 5xx 或 HTML 错误页（如 Cloudflare 502 整页 HTML）
    不塞进界面，只给一句可读提示；其余（代理返回的 JSON 业务错误）保留原文便于定位。"""
    b = (body or "").lstrip()
    is_html = b[:15].lower().startswith("<!doctype html") or b[:5].lower() == "<html"
    if status_code in (502, 503, 504) or is_html:
        return f"Gemini 代理暂不可用（网关 {status_code}），源站可能正在重启，请稍后重试"
    return f"Gemini 代理失败 {status_code}: {body[:300]}"


async def _analyze_one_part_gemini(part: AdmakePart, on_stage=None) -> dict:
    """Compress video → upload to US OSS → call Dscript Gemini proxy → return timestamps.
    Returns {"parsed": dict, "raw": str, "orig_bytes": int, "comp_bytes": int, "comp_skip_bytes": int}.
    on_stage: 可选回调，在 compressing/uploading/analyzing 三个阶段切换时上报（进度展示用）。"""
    proxy_url = settings.GEMINI_PROXY_URL
    proxy_key = settings.GEMINI_PROXY_KEY
    if not proxy_url:
        raise RuntimeError("未配置 GEMINI_PROXY_URL，请在 .env 中填写 Dscript 代理接口地址")

    abs_src = os.path.join(settings.UPLOAD_DIR, part.file_path) if not os.path.isabs(part.file_path) else part.file_path
    compressed = os.path.join(_ai_tmp_dir(), f"ai_tmp_gemini_{part.id}.mp4")
    orig_bytes = os.path.getsize(abs_src)
    comp_bytes = 0
    oss_key = None

    try:
        # 1. 压缩视频
        if on_stage:
            on_stage("compressing")
        _t = _time.monotonic()
        _, comp_skip_bytes = await vid_svc.compress_for_ai(abs_src, compressed)
        compress_ms = int((_time.monotonic() - _t) * 1000)
        comp_bytes = os.path.getsize(compressed)

        # 2. 上传到美区 OSS
        if on_stage:
            on_stage("uploading")
        _t = _time.monotonic()
        oss_url, oss_key = await _upload_to_oss_us(compressed)
        upload_ms = int((_time.monotonic() - _t) * 1000)

        # 3. 调用 Dscript Gemini 代理
        if on_stage:
            on_stage("analyzing")
        payload = {
            "part_name": part.name or f"part_{part.id}",
            "oss_url": oss_url,
            "prompt": AI_ANALYSIS_PROMPT,
        }
        headers = {"Content-Type": "application/json"}
        if proxy_key:
            headers["X-SkyArt-Key"] = proxy_key

        # 源站间歇宕机时对 POST 退避重试：压缩+上传已在上面做过一次，这里只重发请求，
        # 复用同一个 oss_url，不重复烧跨境上传。4xx（鉴权/地区/审核）明确拒绝，不重试。
        _t = _time.monotonic()
        attempts = 0
        resp = None
        last_proxy_err = None
        for backoff in [0, *GEMINI_PROXY_RETRY_BACKOFF_SEC]:
            if backoff:
                if on_stage:
                    on_stage("analyzing")  # 保持进度态，退避期间不误报失败
                await asyncio.sleep(backoff)
            try:
                attempts += 1
                async with httpx.AsyncClient(timeout=httpx.Timeout(360.0)) as client:
                    resp = await client.post(proxy_url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_proxy_err = RuntimeError(f"Gemini 代理连接失败: {type(e).__name__}: {str(e)[:200]}")
                resp = None
                continue  # 网络层失败：退避后重试
            if resp.is_success:
                break
            if 400 <= resp.status_code < 500:
                # 4xx 明确拒绝，重试无意义，直接抛
                raise RuntimeError(_proxy_err_msg(resp.status_code, resp.text))
            # 5xx（含源站宕机的 Cloudflare 502）：记录后退避重试
            last_proxy_err = RuntimeError(_proxy_err_msg(resp.status_code, resp.text))
            resp = None
        if resp is None or not resp.is_success:
            # 退避重试用尽仍失败：标记 no_retry，外层别再重压缩+上传（这里已充分重试）
            err = last_proxy_err or RuntimeError("Gemini 代理重试用尽")
            err.no_retry = True
            raise err
        analyze_ms = int((_time.monotonic() - _t) * 1000)

        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Gemini 代理返回错误 [{data.get('error_code')}]: {data.get('message', '')}")

        # 4. 代理已返回结构化数据，直接使用
        parsed = {
            "highlights": data.get("highlights", []),
            "hooks": data.get("hooks", []),
        }
        raw = json.dumps(parsed, ensure_ascii=False)
        # per-stage 耗时日志：定位慢在压缩/上传/分析哪一段（analyze 含代理重试次数）
        log_event("admake.ai_analyze.stages", part_id=part.id,
                  compress_ms=compress_ms, upload_ms=upload_ms,
                  analyze_ms=analyze_ms, attempts=attempts)
        return {
            "parsed": parsed,
            "raw": raw,
            "orig_bytes": orig_bytes,
            "comp_bytes": comp_bytes,
            "comp_skip_bytes": comp_skip_bytes,
        }

    finally:
        try:
            os.remove(compressed)
        except Exception:
            pass
        if oss_key:
            asyncio.create_task(_delete_oss_us(oss_key))


async def _run_ai_analyze(project_id: int, part_ids: list[int], ai_model: str = "gemini-3.5-flash", project_title: str = ""):
    from app.database import AsyncSessionLocal
    _ai_tasks[project_id] = {
        "status": "processing", "progress": 0, "total": len(part_ids), "error": "",
        # 进度展示：active_parts=[{name,stage}]（流水线并发，可能同时多个在处理），eta_seconds=预估剩余
        "active_parts": [], "eta_seconds": None,
        # 已完成的 Part 各自挑出了几个高光/钩子、用了多久。这几个数原来只写进日志，
        # 界面上只说"成功 6/7"，不说做出了什么——而标记才是这次分析的产物。
        "finished_parts": [],
    }
    last_error = ""
    success_count = 0
    errors: list[str] = []
    part_durations: list[float] = []      # 已完成 Part 的耗时，用于估算剩余时间
    active: dict[int, dict] = {}          # part_id -> {name, stage}，进行中的 Part
    done_counter = {"n": 0}
    db_write_lock = asyncio.Lock()        # SQLite 单写者：并发保存标记时避免 database is locked

    def _refresh_progress():
        remaining = len(part_ids) - done_counter["n"]
        _ai_tasks[project_id]["progress"] = done_counter["n"]
        # stage_since_ts=阶段开始的 epoch 时间：状态接口读取时实时算"该阶段已持续多久"，
        # 让用户/排查者看出卡在哪一步、卡了多久（阶段本身可能几分钟不触发刷新）
        _ai_tasks[project_id]["active_parts"] = [
            {"name": v["name"], "stage": v["stage"], "stage_since_ts": v["stage_since_ts"]}
            for v in active.values()
        ]
        if not remaining:
            _ai_tasks[project_id]["eta_seconds"] = 0
        elif part_durations:
            avg = sum(part_durations) / len(part_durations)
            _ai_tasks[project_id]["eta_seconds"] = int(avg * remaining / min(AI_ANALYZE_CONCURRENCY, remaining))

    # Create log file
    try:
        log_path = _get_log_batch_path(project_id, project_title) if project_title else None
    except Exception as e:
        log_failure("admake.ai_analyze.log_path", e, project_id=project_id)
        log_path = None
    batch_name = os.path.splitext(os.path.basename(log_path))[0] if log_path else "unknown"
    run_start = _time.monotonic()

    def _wlog(text: str):
        # 诊断日志尽力而为：磁盘满/目录被删时写失败绝不能让分析任务
        # 卡死在 processing（_wlog 在 finally 里被调用，异常会穿透 gather）
        if not log_path:
            return
        try:
            with open(log_path, "a", encoding="utf-8") as _f:
                _f.write(text)
        except Exception as e:
            log_failure("admake.ai_analyze.wlog", e, project_id=project_id)

    started_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _wlog(
        f"{'=' * 80}\n"
        f"AI Analysis Log\n"
        f"{'=' * 80}\n"
        f"Project  : {project_title} (id={project_id})\n"
        f"Batch    : {batch_name}\n"
        f"Started  : {started_str}\n"
        # ai_model 是分支开关（gemini 走代理 / 其余走通义），不是实际型号：Gemini 分支
        # 的请求体不带 model 字段，跑哪个版本由 Dscript 代理定。照抄它会让日志读起来
        # 像实测记录，所以这里写实际能确定的那部分。
        f"AI Model : {'Gemini（Dscript 代理，版本以代理为准）' if ai_model.startswith('gemini') else QWEN_VL_MODEL}\n"
        f"Parts    : {len(part_ids)}\n"
        f"{'=' * 80}\n\n"
    )

    sem = asyncio.Semaphore(AI_ANALYZE_CONCURRENCY)

    async def _process_part(i: int, part_id: int):
        nonlocal last_error, success_count

        async def _set_ai_status(st: str, err: str | None = None):
            # AI 分析状态落库（技术债 #3）：后端重启/隔天回看仍能查到失败痕迹
            async with db_write_lock:
                async with AsyncSessionLocal() as _db:
                    _r = await _db.execute(select(AdmakePart).where(AdmakePart.id == part_id))
                    _p = _r.scalar_one_or_none()
                    if _p:
                        _p.ai_status = st
                        if err is not None:
                            _p.ai_error = err[:500]
                        elif st != "failed":
                            _p.ai_error = None  # 成功/重跑时清掉上次失败残留，避免脏数据
                        await _db.commit()

        async with sem:
            part_start = _time.monotonic()
            lines: list[str] = []  # 本 Part 的日志块，结束时一次性写入，避免并发任务的日志交错
            try:
                async with AsyncSessionLocal() as db:
                    res = await db.execute(select(AdmakePart).where(AdmakePart.id == part_id))
                    part = res.scalar_one_or_none()
                if not part or not part.file_path:
                    lines.append(f"[Part {i+1}/{len(part_ids)}] id={part_id} — 跳过（无文件）\n\n")
                    return
                active[part_id] = {"name": part.name or f"Part{i + 1}", "stage": "compressing",
                                   "stage_since_ts": _time.time()}
                _refresh_progress()
                await _set_ai_status("analyzing")

                def _set_stage(stage: str):
                    if part_id in active:
                        active[part_id]["stage"] = stage
                        active[part_id]["stage_since_ts"] = _time.time()
                        _refresh_progress()

                lines.append(
                    f"[Part {i+1}/{len(part_ids)}] {part.name} (id={part_id})\n"
                    f"File       : {part.file_path}\n"
                )

                MAX_RETRIES = 3
                result = None
                for attempt in range(MAX_RETRIES):
                    try:
                        if ai_model.startswith("gemini"):
                            result = await _analyze_one_part_gemini(part, on_stage=_set_stage)
                        else:
                            result = await _analyze_one_part(part, on_stage=_set_stage)
                        break
                    except Exception as retry_err:
                        # 内层已对 5xx/网络做过退避重试（复用 OSS 上传），别在外层重压缩重传
                        if getattr(retry_err, "no_retry", False):
                            raise
                        # 4xx = 明确拒绝（地区限制/鉴权失败），重试无意义，直接抛出
                        if re.search(r'\b4\d{2}\b', str(retry_err)):
                            raise
                        if attempt < MAX_RETRIES - 1:
                            lines.append(f"  第 {attempt + 1} 次失败，{2}s 后重试... 错误: {str(retry_err)[:120]}\n")
                            await asyncio.sleep(2)
                        else:
                            raise

                parsed          = result["parsed"]
                raw             = result["raw"]
                orig_bytes      = result["orig_bytes"]
                comp_bytes      = result["comp_bytes"]
                comp_skip_bytes = result.get("comp_skip_bytes", 0)

                highlights = parsed.get("highlights", [])
                hooks      = parsed.get("hooks", [])
                async with db_write_lock:
                    await _save_ai_marks(part_id, highlights, hooks, part.duration_sec)

                elapsed  = _time.monotonic() - part_start
                orig_mb  = orig_bytes / 1024 / 1024

                if comp_skip_bytes > 0:
                    skip_mb    = comp_skip_bytes / 1024 / 1024
                    skip_ratio = comp_skip_bytes / orig_bytes * 100 if orig_bytes > 0 else 0.0
                    comp_line  = (
                        f"Compressed : 跳过 — ffmpeg 输出 {skip_mb:.1f} MB"
                        f" ({skip_ratio:.1f}% of original, 体积增大)，直接使用原始文件\n"
                    )
                else:
                    comp_mb  = comp_bytes / 1024 / 1024
                    ratio    = comp_bytes / orig_bytes * 100 if orig_bytes > 0 else 0.0
                    saved_mb = orig_mb - comp_mb
                    comp_line = (
                        f"Compressed : {comp_mb:.1f} MB  ({ratio:.1f}% of original, saved {saved_mb:.1f} MB)\n"
                    )

                lines.append(
                    f"Original   : {orig_mb:.1f} MB\n"
                    f"{comp_line}"
                    f"\nRaw AI Response:\n{raw}\n\n"
                    f"Parsed     : highlights={len(highlights)}, hooks={len(hooks)}\n"
                    f"Status     : OK\n"
                    f"Elapsed    : {elapsed:.1f} sec\n"
                    f"{'-' * 80}\n\n"
                )
                success_count += 1
                await _set_ai_status("done")
                # ETA 样本只取成功的 Part：跳过/秒级失败的 Part 耗时近 0，
                # 混入平均值会把预估剩余时间拉到失真的几秒
                part_durations.append(elapsed)
                _ai_tasks[project_id]["finished_parts"].append({
                    "name": part.name or f"Part{i + 1}", "highlights": len(highlights),
                    "hooks": len(hooks), "elapsed": int(elapsed),
                })
                log_event("admake.ai_analyze", part_id=part_id, status="ok",
                          highlights=len(highlights), hooks=len(hooks), elapsed_ms=int(elapsed * 1000))

            except Exception as e:
                elapsed    = _time.monotonic() - part_start
                last_error = str(e)[:300]
                errors.append(f"Part id={part_id}: {last_error}")
                lines.append(
                    f"Status     : FAILED\n"
                    f"Error      : {last_error}\n"
                    f"Elapsed    : {elapsed:.1f} sec\n"
                    f"{'-' * 80}\n\n"
                )
                log_failure("admake.ai_analyze", e, part_id=part_id)
                await _set_ai_status("failed", last_error)
            finally:
                active.pop(part_id, None)
                done_counter["n"] += 1
                _refresh_progress()
                _wlog("".join(lines))

    try:
        await asyncio.gather(*[_process_part(i, pid) for i, pid in enumerate(part_ids)])
    except Exception as e:
        # 兜底：单 Part 的异常已在 _process_part 内消化，这里只会是意料外的代码错误。
        # 必须写入终态，否则前端会永远轮询"分析中"且 beforeunload 拦截无法解除
        log_failure("admake.ai_analyze.batch", e, project_id=project_id)
        _ai_tasks[project_id]["status"] = "failed"
        _ai_tasks[project_id]["error"] = f"内部错误：{str(e)[:200]}"
        _ai_tasks[project_id]["success"] = success_count
        _ai_tasks[project_id]["elapsed_seconds"] = int(_time.monotonic() - run_start)
        _ai_tasks[project_id]["active_parts"] = []
        return

    total_elapsed = _time.monotonic() - run_start
    summary = (
        f"{'=' * 80}\n"
        f"Summary\n"
        f"{'=' * 80}\n"
        f"Total parts   : {len(part_ids)}\n"
        f"Success       : {success_count}\n"
        f"Failed        : {len(errors)}\n"
        f"Total elapsed : {total_elapsed:.1f} sec\n"
    )
    if errors:
        summary += "Errors        :\n" + "\n".join(f"  - {e}" for e in errors) + "\n"
    summary += f"{'=' * 80}\n"
    _wlog(summary)

    _ai_tasks[project_id]["progress"] = len(part_ids)
    _ai_tasks[project_id]["status"] = "done" if not last_error else "partial"
    _ai_tasks[project_id]["error"] = last_error
    _ai_tasks[project_id]["success"] = success_count
    _ai_tasks[project_id]["elapsed_seconds"] = int(total_elapsed)
    log_event("admake.ai_analyze.done", project_id=project_id, total=len(part_ids),
              success=success_count, failed=len(errors),
              elapsed_ms=int(total_elapsed * 1000), status=_ai_tasks[project_id]["status"])


# ── 任务中心：该用户所有进行中的任务（导出/AI 分析/Part 合成），完成即从列表消失 ──

@router.get("/tasks", response_model=list[AdmakeTaskOut])
async def list_running_tasks(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows: list[AdmakeTaskOut] = []
    # 导出中
    res = await db.execute(
        select(AdmakeProject.id, AdmakeProject.title, func.count(AdmakeExport.id))
        .join(AdmakePart, AdmakePart.project_id == AdmakeProject.id)
        .join(AdmakeExport, AdmakeExport.part_id == AdmakePart.id)
        .where(AdmakeProject.user_id == user.id,
               AdmakeExport.status.in_(("pending", "processing")))
        .group_by(AdmakeProject.id, AdmakeProject.title)
    )
    for pid, title, n in res.all():
        rows.append(AdmakeTaskOut(type="export", project_id=pid, title=title, count=n))
    # AI 分析中
    res = await db.execute(
        select(AdmakeProject.id, AdmakeProject.title, func.count(AdmakePart.id))
        .join(AdmakePart, AdmakePart.project_id == AdmakeProject.id)
        .where(AdmakeProject.user_id == user.id, AdmakePart.ai_status == "analyzing")
        .group_by(AdmakeProject.id, AdmakeProject.title)
    )
    for pid, title, n in res.all():
        rows.append(AdmakeTaskOut(type="ai", project_id=pid, title=title, count=n))
    # Part 合成中
    res = await db.execute(
        select(AdmakeProject.id, AdmakeProject.title, func.count(AdmakePart.id))
        .join(AdmakePart, AdmakePart.project_id == AdmakeProject.id)
        .where(AdmakeProject.user_id == user.id,
               AdmakePart.status.in_(("pending", "processing")))
        .group_by(AdmakeProject.id, AdmakeProject.title)
    )
    for pid, title, n in res.all():
        rows.append(AdmakeTaskOut(type="parts", project_id=pid, title=title, count=n))
    return rows


# ── Projects CRUD ─────────────────────────────────────────────────────────────

def _project_all_done(p: AdmakeProject) -> bool:
    """全部任务完成：有 Part 且每个 Part 都合成完、AI 分析完。要求 p.parts 已预加载。"""
    return bool(p.parts) and all(
        pt.status == "ready" and pt.ai_status == "done" for pt in p.parts
    )


@router.get("/projects", response_model=list[AdmakeProjectOut])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(AdmakeProject)
        .options(selectinload(AdmakeProject.sources), selectinload(AdmakeProject.parts))
        .where(AdmakeProject.user_id == user.id)
        .order_by(AdmakeProject.updated_at.desc())
    )
    projects = res.scalars().all()
    out = []
    for p in projects:
        o = AdmakeProjectOut.model_validate(p)
        o.source_count = len(p.sources)
        o.part_count   = len(p.parts)
        o.all_done     = _project_all_done(p)
        out.append(o)
    return out


@router.post("/projects", response_model=AdmakeProjectOut, status_code=201)
async def create_project(
    body: AdmakeProjectCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _validate_naming_field(body.title, "项目标题", _TITLE_MAX_LEN, allow_mid_colon=True)
    if body.editor_alias:
        _validate_naming_field(body.editor_alias, "导出文件命名", 20, allow_mid_colon=False)
    p = AdmakeProject(**body.model_dump(), user_id=user.id)
    if p.export_path is not None and not p.export_path.strip():
        p.export_path = None  # 空字符串归一成 None，与默认目录一致
    db.add(p)
    await db.flush()
    await db.refresh(p)
    out = AdmakeProjectOut.model_validate(p)
    out.source_count = 0
    out.part_count   = 0
    return out


@router.get("/projects/{pid}", response_model=AdmakeProjectDetailOut)
async def get_project(
    pid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(AdmakeProject)
        .options(selectinload(AdmakeProject.sources), selectinload(AdmakeProject.parts))
        .where(AdmakeProject.id == pid, AdmakeProject.user_id == user.id)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在或无权限")
    out = AdmakeProjectDetailOut.model_validate(p)
    out.source_count = len(p.sources)
    out.part_count   = len(p.parts)
    out.all_done     = _project_all_done(p)
    out.sources = [AdmakeSourceOut.model_validate(s) for s in p.sources]
    out.parts   = [AdmakePartOut.model_validate(pt) for pt in p.parts]
    return out


@router.get("/projects/{pid}/export-dir")
async def get_export_dir(
    pid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回本项目当前实际的导出目录（绝对路径，顺带建好）。
    目录规则（自定义目录下剧名子文件夹 / 默认目录带 id）只在 _project_exports_dir 一处，
    前端「打开文件夹」直接用这里的结果，不自己拼路径。"""
    p = await _get_project(pid, user, db)
    d = await asyncio.to_thread(_project_exports_dir, p.id, p.title, p.export_path)
    return {"dir": d, "is_custom": bool(p.export_path)}


@router.put("/projects/{pid}", response_model=AdmakeProjectOut)
async def update_project(
    pid: int,
    body: AdmakeProjectUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(AdmakeProject)
        .options(selectinload(AdmakeProject.sources), selectinload(AdmakeProject.parts))
        .where(AdmakeProject.id == pid, AdmakeProject.user_id == user.id)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在或无权限")
    if body.title is not None:
        _validate_naming_field(body.title, "项目标题", _TITLE_MAX_LEN, allow_mid_colon=True)
    if body.editor_alias:
        _validate_naming_field(body.editor_alias, "导出文件命名", 20, allow_mid_colon=False)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    # 前端用 0 表示"不套模板"：清空默认模板（None 与"未传"无法区分，故用 0 当哨兵）
    if p.default_template_id == 0:
        p.default_template_id = None
    # 空字符串 = 清空自定义导出目录，回到默认目录（"" 不被 exclude_none 过滤，可当哨兵）
    if p.export_path is not None and not p.export_path.strip():
        p.export_path = None
    await db.flush()
    out = AdmakeProjectOut.model_validate(p)
    out.source_count = len(p.sources)
    out.part_count   = len(p.parts)
    return out


@router.delete("/projects/{pid}", status_code=204)
async def delete_project(
    pid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = await _get_project(pid, user, db)

    # 有任务进行中的项目不能删：后台任务正拿着这个项目的文件在写，删表+删目录会让它
    # 中途报错，还可能把半成品留在导出目录。判定口径与任务中心 GET /tasks 一致。
    part_ids_res = await db.execute(select(AdmakePart.id).where(AdmakePart.project_id == pid))
    part_ids = [r[0] for r in part_ids_res.all()]
    busy_checks = [
        ("导出", select(func.count(AdmakeExport.id)).where(
            AdmakeExport.status.in_(("pending", "processing")),
            or_(AdmakeExport.project_id == pid,
                AdmakeExport.part_id.in_(part_ids) if part_ids else false()))),
        ("Part 合成", select(func.count(AdmakePart.id)).where(
            AdmakePart.project_id == pid, AdmakePart.status.in_(("pending", "processing")))),
        ("AI 分析", select(func.count(AdmakePart.id)).where(
            AdmakePart.project_id == pid, AdmakePart.ai_status == "analyzing")),
    ]
    for label, q in busy_checks:
        if (await db.execute(q)).scalar_one():
            raise HTTPException(409, f"「{p.title}」有{label}任务进行中，等它完成后再删除")
    if _ai_tasks.get(pid, {}).get("status") == "processing":
        raise HTTPException(409, f"「{p.title}」有 AI 分析任务进行中，等它完成后再删除")

    proj_slug = f"project_{p.id}_{_slug(p.title)}"
    # Explicit bulk deletes — avoids async lazy-load cascade (MissingGreenlet)
    if part_ids:
        await db.execute(delete(AdmakeMark).where(AdmakeMark.part_id.in_(part_ids)))
        await db.execute(delete(AdmakeExport).where(AdmakeExport.part_id.in_(part_ids)))
        await db.execute(delete(AdmakePart).where(AdmakePart.project_id == pid))
    await db.execute(delete(AdmakeSource).where(AdmakeSource.project_id == pid))
    await db.execute(delete(AdmakeProject).where(AdmakeProject.id == pid))
    for type_dir in ("Source", "Parts", "Export"):
        d = os.path.join(settings.UPLOAD_DIR, "AdMake", type_dir, proj_slug)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)


# ── Sources ───────────────────────────────────────────────────────────────────

@router.post("/projects/{pid}/sources", response_model=list[AdmakeSourceOut])
async def upload_sources(
    pid: int,
    files: list[UploadFile] = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = await _get_project(pid, user, db)
    sources_dir = _project_sources_dir(p.id, p.title)
    res = await db.execute(
        select(AdmakeSource).where(AdmakeSource.project_id == pid).order_by(AdmakeSource.sort_order.desc())
    )
    existing = res.scalars().all()
    next_order = (existing[0].sort_order + 1) if existing else 0

    created: list[AdmakeSource] = []
    for i, f in enumerate(files):
        order = next_order + i
        orig_name = f.filename or f"video_{order}.mp4"
        safe_name = re.sub(r'[^\w一-鿿\-\.]', '_', orig_name)[:100]
        save_name = f"{order:02d}_{safe_name}"
        save_path = os.path.join(sources_dir, save_name)
        content = await f.read()
        with open(save_path, "wb") as fp:
            fp.write(content)
        src = AdmakeSource(
            project_id=pid,
            original_filename=orig_name,
            file_path=os.path.relpath(save_path, settings.UPLOAD_DIR),
            sort_order=order,
        )
        db.add(src)
        created.append(src)

    await db.flush()
    for src in created:
        await db.refresh(src)

    async def _probe_all():
        from app.database import AsyncSessionLocal
        for src in created:
            try:
                abs_p = os.path.join(settings.UPLOAD_DIR, src.file_path)
                info = await vid_svc.get_video_info(abs_p)
                # 片头/片尾转场段检测（定格/静图缓拉）：当前只标注不裁剪，供人工核对准确率
                try:
                    head_t, tail_t = await asyncio.to_thread(
                        detect_transitions, abs_p, info["duration"])
                except Exception as e2:
                    head_t = tail_t = 0.0
                    log_failure("admake.transition_detect", e2, source_id=src.id)
                async with AsyncSessionLocal() as db2:
                    res2 = await db2.execute(select(AdmakeSource).where(AdmakeSource.id == src.id))
                    s2 = res2.scalar_one_or_none()
                    if s2:
                        s2.duration_sec = info["duration"]
                        s2.width, s2.height, s2.fps = info["width"], info["height"], info["fps"]
                        s2.head_trim, s2.tail_trim = head_t, tail_t
                        await db2.commit()
                # 检出 0 也要留痕。原来只在检出非零时打日志，排查漏剪时看不出这条素材是
                # "检测没跑"还是"跑了但判定不剪"——2026-08-11 查一集定格没剪掉，日志里
                # 那条素材干净得像没处理过，只能靠翻数据库的 tail_trim 才确认检测跑过。
                log_event("admake.transition_detected", source_id=src.id,
                          file=src.original_filename, head_trim=head_t, tail_trim=tail_t,
                          detected=bool(head_t or tail_t))
            except Exception as e:
                # 探测失败时 duration_sec 保持列默认值 0——build_parts 的 needs_probe
                # 会按 duration_sec<=0 重新探测一次并在再次失败时明确报错中断，
                # 这里只负责留痕，不能吞掉后就当作"探测过了"。
                log_failure("admake.upload_probe_duration", e, source_id=src.id, file_path=src.file_path)

    background_tasks.add_task(_probe_all)
    return [AdmakeSourceOut.model_validate(s) for s in created]


@router.delete("/projects/{pid}/sources/{sid}", status_code=204)
async def delete_source(
    pid: int, sid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_project(pid, user, db)
    res = await db.execute(
        select(AdmakeSource).where(AdmakeSource.id == sid, AdmakeSource.project_id == pid)
    )
    src = res.scalar_one_or_none()
    if not src:
        raise HTTPException(404, "源文件不存在")
    abs_path = os.path.join(settings.UPLOAD_DIR, src.file_path) if src.file_path else None
    await db.delete(src)
    await db.flush()
    if abs_path and os.path.isfile(abs_path):
        os.remove(abs_path)


# ── Parts ─────────────────────────────────────────────────────────────────────

_MAX_PART_SEC = 6 * 60
_TARGET_PART_SEC = 5 * 60   # 超长单源切片时的目标时长，约 5 分钟


def _expand_groups(sources: list) -> list[tuple[list, float | None, float | None]]:
    """把源视频分组展开成 Part 计划表，每项是 (源列表, 起点秒或 None, 终点秒或 None)。

    单源超过 _MAX_PART_SEC 的会被切成多段，其余按 _group_into_parts 的结果原样成组。
    结果只取决于源列表，同样的输入必然得到同样的计划表——单个 Part 重建就是靠这一点
    复算出它当初该用哪些源、切哪一段，不必额外落库。
    """
    expanded: list[tuple[list, float | None, float | None]] = []
    for group in _group_into_parts(sources):
        eff = _eff_dur(group[0])
        if len(group) == 1 and eff > _MAX_PART_SEC:
            src = group[0]
            n_chunks = max(2, math.ceil(eff / _TARGET_PART_SEC))
            chunk_dur = eff / n_chunks
            for c in range(n_chunks):
                # 最后一段的终点就是有效时长，片尾板自然被切在外面
                expanded.append(([src], c * chunk_dur, min(eff, (c + 1) * chunk_dur)))
        else:
            expanded.append((group, None, None))
    return expanded


def _dispatch_part_build(background_tasks: BackgroundTasks, part_id: int,
                         group: list, start_sec: float | None, end_sec: float | None) -> None:
    """按计划表里的一项派发合成任务：切片 / 单源复制 / 多源拼接。"""
    if start_sec is not None:
        # 切片路径的终点已经按有效时长算过，这里不用再传截断点
        background_tasks.add_task(_cut_part_segment, part_id, group[0].file_path, start_sec, end_sec)
        return
    # 拼接和单源复制都走 merge_videos，按源逐个传"只取前多少秒"。
    # 一个 Part 里可能拼了两三集，每一集的结尾都有自己的片尾板，所以是逐源截断。
    limits = [(_eff_dur(s) if (s.tail_trim or 0) > 0 else None) for s in group]
    if len(group) == 1:
        background_tasks.add_task(_copy_source_as_part, part_id, group[0].file_path,
                                  _eff_dur(group[0]), limits[0])
    else:
        background_tasks.add_task(_build_part, part_id, [s.file_path for s in group], limits)


@router.post("/projects/{pid}/build-parts", response_model=list[AdmakePartOut])
async def build_parts(
    pid: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(AdmakeProject)
        .options(selectinload(AdmakeProject.sources), selectinload(AdmakeProject.parts))
        .where(AdmakeProject.id == pid, AdmakeProject.user_id == user.id)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在或无权限")
    if not p.sources:
        raise HTTPException(400, "请先上传源视频")

    # Probe duration+resolution+fps for any source that hasn't been probed yet.
    # A failed probe must NOT silently leave duration_sec at 0 — a 0-duration source
    # defeats _group_into_parts' MAX_SEC cap (0 always fits), letting unrelated sources
    # pile into one unbounded group. Abort instead so the bad upload gets fixed/re-uploaded.
    # duration_sec 同时也是"探测跑完了没有"的标记：上传接口的后台任务把时长、
    # 分辨率、片头片尾裁剪点写在同一次提交里，duration_sec>0 就说明这三样都齐了。
    # 前端上传成功后立刻发 build-parts，而转场检测每条源要抽 120 帧、耗时以秒计，
    # 稳定输给这个请求——所以这里读到的 tail_trim 必然是 0，片尾定格板一段都截不掉。
    # 探测没跑完就自己补一遍，包括转场检测，不再指望后台任务先到。
    needs_probe = [s for s in p.sources if s.duration_sec <= 0]
    for src in needs_probe:
        try:
            abs_p = os.path.join(settings.UPLOAD_DIR, src.file_path)
            info = await vid_svc.get_video_info(abs_p)
            src.duration_sec = info["duration"]
            src.width, src.height, src.fps = info["width"], info["height"], info["fps"]
            try:
                head_t, tail_t = await asyncio.to_thread(
                    detect_transitions, abs_p, info["duration"])
                src.head_trim, src.tail_trim = head_t, tail_t
                log_event("admake.transition_detected", source_id=src.id,
                          file=src.original_filename, head_trim=head_t, tail_trim=tail_t,
                          detected=bool(head_t or tail_t), where="build_parts")
            except Exception as e2:
                # 检测失败只是不裁片尾，不该拖垮建 Part
                log_failure("admake.transition_detect", e2, source_id=src.id)
        except Exception as e:
            log_failure("admake.probe_duration", e, source_id=src.id, file_path=src.file_path)
            raise HTTPException(400, f"素材时长探测失败，文件可能已损坏：{src.file_path}")
    if needs_probe:
        await db.flush()

    # 一行结构化摘要：项目内所有素材的分辨率/帧率分布，混用规格时肉眼可见，
    # 不必再靠日志乱码逆向排查（参见 2026-07-02 的 Part 卡顿/音画不同步事故）。
    log_event(
        "admake.build_parts.sources", project_id=pid, n_sources=len(p.sources),
        resolutions=[f"{s.width}x{s.height}" for s in p.sources],
        fps_list=[s.fps for s in p.sources],
    )

    for old_part in p.parts:
        await db.delete(old_part)
    await db.flush()

    expanded = _expand_groups(list(p.sources))

    new_parts: list[AdmakePart] = []
    for i, (group, start_sec, end_sec) in enumerate(expanded):
        if start_sec is not None:
            est_dur = end_sec - start_sec
            status = "processing"
        else:
            est_dur = sum(_eff_dur(s) for s in group)
            status = "pending" if len(group) > 1 else "processing"
        part = AdmakePart(
            project_id=pid,
            name=f"drama_Part{i + 1}",
            part_index=i,
            duration_sec=est_dur,
            status=status,
            source_ids=json.dumps([s.id for s in group]),
        )
        db.add(part); new_parts.append(part)

    await db.flush()
    for pt in new_parts:
        await db.refresh(pt)

    for pt, (group, start_sec, end_sec) in zip(new_parts, expanded):
        _dispatch_part_build(background_tasks, pt.id, group, start_sec, end_sec)

    return [AdmakePartOut.model_validate(pt) for pt in new_parts]


@router.get("/projects/{pid}/parts", response_model=list[AdmakePartOut])
async def list_parts(
    pid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_project(pid, user, db)
    res = await db.execute(
        select(AdmakePart).where(AdmakePart.project_id == pid).order_by(AdmakePart.part_index)
    )
    return [AdmakePartOut.model_validate(pt) for pt in res.scalars().all()]


@router.post("/parts/{part_id}/rebuild", response_model=AdmakePartOut)
async def rebuild_part(
    part_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """重跑单个 Part 的合成。

    合成失败以前只能整个项目重新分 Part——七集里挂一集，另外六集连同已做的 AI 标注
    一起重来。这里按 _expand_groups 复算出这个 Part 当初该用哪些源、切哪一段，只重跑它。

    源视频增删过之后计划表会变，`part_index` 指向的可能已经是另一段素材，
    这种情况直接拒绝并要求重新分 Part，不能悄悄按新计划重建、让编号和内容对不上。
    """
    part = await _get_part(part_id, user, db)
    res = await db.execute(
        select(AdmakeProject)
        .options(selectinload(AdmakeProject.sources))
        .where(AdmakeProject.id == part.project_id)
    )
    proj = res.scalar_one_or_none()
    if not proj or not proj.sources:
        raise HTTPException(400, "项目没有源视频，无法重建")

    expanded = _expand_groups(list(proj.sources))
    if part.part_index >= len(expanded):
        raise HTTPException(409, "源视频已变化，请重新分 Part")

    group, start_sec, end_sec = expanded[part.part_index]
    if [s.id for s in group] != json.loads(part.source_ids or "[]"):
        raise HTTPException(409, "源视频已变化，请重新分 Part")

    part.status = "processing"
    part.file_path = None
    await db.commit()
    await db.refresh(part)

    _dispatch_part_build(background_tasks, part.id, group, start_sec, end_sec)
    log_event("admake.rebuild_part", part_id=part.id, project_id=part.project_id,
              part_index=part.part_index, n_sources=len(group))
    return AdmakePartOut.model_validate(part)


# ── Marks ─────────────────────────────────────────────────────────────────────

@router.get("/parts/{part_id}/marks", response_model=list[AdmakeMarkOut])
async def get_marks(
    part_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_part(part_id, user, db)
    res = await db.execute(
        select(AdmakeMark)
        .where(AdmakeMark.part_id == part_id)
        .order_by(AdmakeMark.group_index, AdmakeMark.time_sec)
    )
    return [AdmakeMarkOut.model_validate(m) for m in res.scalars().all()]


@router.put("/parts/{part_id}/marks", response_model=list[AdmakeMarkOut])
async def save_marks(
    part_id: int,
    body: AdmakeMarkBatchSave,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_part(part_id, user, db)
    await db.execute(delete(AdmakeMark).where(AdmakeMark.part_id == part_id))
    new_marks: list[AdmakeMark] = []
    for m in body.marks:
        mark = AdmakeMark(part_id=part_id, **m.model_dump())
        db.add(mark); new_marks.append(mark)
    await db.flush()
    for m in new_marks:
        await db.refresh(m)
    # 留痕：曾出现"新项目 Part 无来由带上了带评分标记"（2026-07-03），HTTP 日志看不到
    # 请求体、无法定位来源，这里记录条数和是否带评分，便于复现时追踪
    log_event("admake.marks_saved", part_id=part_id, n_marks=len(new_marks),
              n_scored=sum(1 for m in new_marks if m.score is not None))
    return [AdmakeMarkOut.model_validate(m) for m in new_marks]


# ── Export ────────────────────────────────────────────────────────────────────

@router.post("/parts/{part_id}/export", response_model=AdmakeExportOut, status_code=201)
async def create_export(
    part_id: int,
    body: AdmakeExportRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    part = await _get_part(part_id, user, db)
    # 未知模式在 _build_segments 里只会得到空片段，报出来的是"标记点不完整"，把人引去查标记。
    if body.mode not in _EXPORT_MODES:
        raise HTTPException(400, f"未知的导出模式：{body.mode}")
    if part.status != "ready" or not part.file_path:
        raise HTTPException(400, "Part 尚未就绪，请先等待 Part 视频合并完成")

    res = await db.execute(
        select(AdmakeMark)
        .where(AdmakeMark.part_id == part_id)
        .order_by(AdmakeMark.group_index, AdmakeMark.time_sec)
    )
    marks = res.scalars().all()
    if not marks:
        raise HTTPException(400, "请先保存标记点再导出")

    segments = _build_segments(list(marks), body.mode)
    if not segments:
        raise HTTPException(400, "标记点不完整，每组需有高光开始/结束和钩子开始/结束")

    proj_res = await db.execute(select(AdmakeProject).where(AdmakeProject.id == part.project_id))
    project = proj_res.scalar_one()

    export = AdmakeExport(part_id=part_id, mode=body.mode, status="pending")
    db.add(export)
    await db.flush()
    await db.refresh(export)

    background_tasks.add_task(_run_export, export.id, part.file_path, segments, project.id, project.title,
                              template_id=body.template_id, export_path=project.export_path)
    return AdmakeExportOut.model_validate(export)


@router.get("/parts/{part_id}/exports", response_model=list[AdmakeExportOut])
async def list_exports(
    part_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_part(part_id, user, db)
    res = await db.execute(
        select(AdmakeExport)
        .where(AdmakeExport.part_id == part_id)
        .order_by(AdmakeExport.created_at.desc())
    )
    return [AdmakeExportOut.model_validate(e) for e in res.scalars().all()]


async def _load_selection_inputs(pid: int, part_ids: list[int] | None, db: AsyncSession):
    """读出这次选材要用的标记和有序 Part。导出接口和导出前预检共用这一份。

    两个入口各写一遍的话，预检报的「建议条数」会和真正导出时的口径对不上——
    用户照着建议填，导出报告里却出现「超过不重复上限」（KNOWLEDGE #4 的同类问题）。
    """
    all_highlights, all_hooks = await _collect_all_marks(pid, db)

    # 如果前端传了 part_ids（视频轨上的 Part），只使用这些 Part 的标记和白场。
    # 先校验归属：项目 id 会被复用（删项目→新项目同 id），前端 localStorage 里的
    # 轨道可能残留旧项目/已重建 Part 的失效 id，直接拿去过滤会把标记筛成空集，
    # 再报"没有标记"就是误导（2026-07-10 实际发生）
    if part_ids:
        own_res = await db.execute(
            select(AdmakePart.id).where(
                AdmakePart.project_id == pid, AdmakePart.id.in_(part_ids)
            )
        )
        allowed = set(own_res.scalars().all())
        if not allowed:
            raise HTTPException(400, "视频轨上的 Part 已失效（项目或 Part 已被重建/删除），请重新拖入 Part 后再导出")
        all_highlights = [h for h in all_highlights if h["part_id"] in allowed]
        all_hooks      = [h for h in all_hooks      if h["part_id"] in allowed]

    if not all_highlights:
        raise HTTPException(400, "没有找到高光标记，请先为分 Part 添加高光标记")
    if not all_hooks:
        raise HTTPException(400, "没有找到钩子标记，请先为分 Part 添加钩子标记")

    # 查询有序 Part 列表，供 _plan_export 填充中间 Part 白场。
    # part_ids 只用来圈定范围，顺序语义一律按 part_index：白场要接的是剧里真实的前后关系，
    # 前端也是按 part_index 排好再传的（_checkedPartsOrdered）。
    parts_res = await db.execute(
        select(AdmakePart)
        .where(AdmakePart.project_id == pid, AdmakePart.status == "ready")
        .order_by(AdmakePart.part_index)
    )
    all_ready = {p.id: p for p in parts_res.scalars().all() if p.file_path}

    if part_ids:
        # 按视频轨顺序排列，过滤不在轨道上的 Part
        parts_ordered = [
            {"part_id": p.id, "part_name": p.name, "part_index": p.part_index,
             "file_path": p.file_path, "part_duration": p.duration_sec}
            for pid_item in part_ids
            if (p := all_ready.get(pid_item))
        ]
    else:
        parts_ordered = [
            {"part_id": p.id, "part_name": p.name, "part_index": p.part_index,
             "file_path": p.file_path, "part_duration": p.duration_sec}
            for p in sorted(all_ready.values(), key=lambda x: x.part_index)
        ]
    return all_highlights, all_hooks, parts_ordered


@router.get("/projects/{pid}/export-preflight")
async def export_preflight(
    pid: int,
    mode: str,
    target_duration: float,
    part_ids: str | None = None,
    template_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """导出前算一次：这个模式加这个目标时长，最多能出几条互不重复的成片。

    以前这个数只写进导出完成后的 .log.txt，用户得先花几十分钟导完 20 条，
    才在报告里看到「其实只有 9 条不重样」。口径与真正导出完全一致（同一个
    _load_selection_inputs 和 _dur_pool_size），不会出现照着建议填还被判超限。

    reachable_min / reachable_max 是现有素材能做出的成片长度范围。目标落在范围外时
    suggested 会虚高——窗口会一路撑大到把所有组合都圈进来，而那些成片长度全挤在一处，
    互不重复也没有意义，所以这种情况直接不给建议值，由 target_in_range 标出来。
    """
    await _get_project(pid, user, db)
    if mode not in _EXPORT_MODES:
        raise HTTPException(400, f"未知的导出模式：{mode}")
    if target_duration <= 0:
        raise HTTPException(400, "目标时长必须大于 0")

    ids = [int(x) for x in part_ids.split(",") if x.strip().isdigit()] if part_ids else None
    try:
        highlights, hooks, parts_ordered = await _load_selection_inputs(pid, ids, db)
    except HTTPException as e:
        # 缺标记之类的前置问题，预检不该弹错误，如实回报让界面提示补标记
        return {"combos": 0, "suggested": 0, "reason": e.detail,
                "reachable_min": None, "reachable_max": None, "target_in_range": False}

    sticker = await _template_sticker_duration(template_id, db)
    body_target = max(target_duration - sticker, _MIN_BODY_SEC)
    scored = _rank_combos(mode, highlights, hooks, parts_ordered, body_target)
    if not scored:
        return {"combos": 0, "suggested": 0, "reason": _no_combo_reason(mode, len(hooks)),
                "reachable_min": None, "reachable_max": None, "target_in_range": False}

    durs = sorted(d for _, d in scored)
    lo, hi = durs[0], durs[-1]
    # 二元判断「目标在不在 lo~hi 之间」太狠：可达范围窄的时候差几秒就把建议值一刀切掉。
    # 改看最贴合的那条离目标多远，超过容差才算错配。
    nearest = min(abs(d - body_target) for d in durs)
    in_range = nearest <= max(body_target * _MISMATCH_RATIO, _MISMATCH_MIN_SEC)
    return {
        "combos": len(scored),
        "suggested": _dur_pool_size(scored, body_target) if in_range else 0,
        "reason": None,
        "reachable_min": round(lo + sticker, 1),
        "reachable_max": round(hi + sticker, 1),
        "target_in_range": in_range,
        "sticker_sec": round(sticker, 1),
    }


# 目标和素材算不算错配：最贴合的那条离目标超过「目标的一半」或 60 秒（取大者）才算。
_MISMATCH_RATIO = 0.5
_MISMATCH_MIN_SEC = 60.0

# 「常用」快捷值取最近多少次导出。太少会被一次异常操作带偏，太多跟不上习惯变化。
_HABIT_RECENT_EXPORTS = 30
_HABIT_MAX_VALUES = 4


@router.get("/export-habits")
async def export_habits(
    mode: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """这个人在这个模式下最常用的几档目标时长，给导出面板做快捷值。

    按人统计不按项目：同一个运营的投放习惯跨剧是稳定的，按项目统计新建项目永远是空的。
    分模式各算各的——悬疑倒置的成片只有几十秒，和顺剪类不是一个量级，混在一起统计
    会给出谁都用不上的值。
    """
    res = await db.execute(
        select(AdmakeExport.target_duration)
        .join(AdmakeProject, AdmakeExport.project_id == AdmakeProject.id)
        .where(AdmakeProject.user_id == user.id,
               AdmakeExport.mode == mode,
               AdmakeExport.target_duration.isnot(None))
        .order_by(AdmakeExport.id.desc())
        .limit(_HABIT_RECENT_EXPORTS)
    )
    # 按分钟归档，不足一分钟的算作 1 分——界面上目标时长的下限就是 1 分钟。
    # 原来写的是 int(round(sec/60)) 且丢掉 0，30 秒的记录会被整个抹掉。
    tally: dict[int, int] = {}
    for (sec,) in res.all():
        minutes = max(1, int(round((sec or 0) / 60)))
        tally[minutes] = tally.get(minutes, 0) + 1
    if len(tally) < 2:          # 记录太少，给默认档位而不是一个孤零零的值
        return {"values": [], "from": "default"}
    top = sorted(tally, key=lambda m: (-tally[m], m))[:_HABIT_MAX_VALUES]
    return {"values": sorted(top), "counts": {str(m): tally[m] for m in top}, "from": "history"}


@router.post("/projects/{pid}/export", response_model=list[AdmakeExportOut], status_code=201)
async def create_project_export(
    pid: int,
    body: AdmakeExportRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await _get_project(pid, user, db)

    # 参数在这里就拦住。模式名拼错的话选材会一路走到后台任务里才因为"没有有效片段"失败，
    # 界面上只看到一条导出失败；条数没有上限的话，界面 max=30 挡不住手输的 500——
    # number 输入框允许输入超出 max 的值，而导出开始后不能中途取消。
    if body.mode not in _EXPORT_MODES:
        raise HTTPException(400, f"未知的导出模式：{body.mode}")
    if not 1 <= body.count <= _MAX_EXPORT_COUNT:
        raise HTTPException(400, f"素材数量需在 1~{_MAX_EXPORT_COUNT} 条之间")
    if body.target_duration <= 0:
        raise HTTPException(400, "目标时长必须大于 0")

    all_highlights, all_hooks, parts_ordered = await _load_selection_inputs(
        pid, body.part_ids, db)

    all_highlights.sort(key=lambda h: h["score"], reverse=True)
    all_hooks.sort(key=lambda h: h["score"], reverse=True)
    # 近似相同的段（同 Part 且 IoU>50%）归成同簇，配对去重/均衡按簇算
    _cluster_marks(all_highlights, "hl")
    _cluster_marks(all_hooks, "hk")
    # 候选数按簇数报：三条 90% 重叠的高光实际只是 1 个素材，日志别虚报成 3
    n_hl = len({_mark_key(h) for h in all_highlights})
    n_hk = len({_mark_key(h) for h in all_hooks})

    # 界面上的目标时长指成片总长，模板前后贴片也占在里面，所以选材要用扣掉贴片之后的
    # 长度当靠拢点。不扣的话贴片有多长、成片就超出多长（2026-08-10：188 秒的片尾让
    # 3 分钟目标的成片全部导成 6 分 12 秒）。
    sticker_dur = await _template_sticker_duration(body.template_id, db)
    body_target = body.target_duration - sticker_dur
    if body_target < _MIN_BODY_SEC:
        log_event("admake.export.target_clamped", project_id=pid, template_id=body.template_id,
                  target_duration=body.target_duration, sticker_sec=round(sticker_dur, 2),
                  clamped_to=_MIN_BODY_SEC)
        body_target = _MIN_BODY_SEC

    # 三种模式统一按「成片时长离目标多近」排候选，界面上的目标时长因此对全部模式生效。
    #
    # 早先只有高光顺剪吃这个值，而且判据是"先看落没落在 [min,max] 区间内，再比离中点的
    # 距离"。两个毛病：界面上三档区间（2-4/5-7/9-12 分）之间留着 4-5 分和 7-9 分两段缝，
    # 落在缝里的配对任何一档都选不中——拿真实项目算过，38 种配对里有 14 种（37%）永远
    # 轮不上；而且"在区间内"是个硬门槛，会让一个刚好卡在中点、评分平平的配对，赢过只差
    # 半分钟但两段都是高分的配对。现在只留一个目标值，每种组合归到离它最近的那一档，
    # 天然不重不漏。
    scored_combos = _rank_combos(body.mode, all_highlights, all_hooks, parts_ordered, body_target)
    n_combos = len(scored_combos)
    submitted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    part_names = [p["part_name"] for p in parts_ordered] if body.part_ids else None

    exports_created: list[tuple[AdmakeExport, list[dict], dict, str]] = []
    combo_plan = _plan_combos(scored_combos, body.count, body_target)

    # 选材决策整条记一遍。只记最终时长的话，成片长度不对时分不清是选材挑错了、目标时长没
    # 生效、还是素材本身够不着——业务同学的机器上没法查数据库，只能靠日志。
    # reachable 是全部候选组合的可达区间：目标落在区间外时，再怎么挑也只能取最接近的一端。
    combo_durs = sorted(d for _, d in scored_combos)
    n_pool = _dur_pool_size(scored_combos, body_target)
    log_event("admake.export.planned", project_id=pid, mode=body.mode, count=body.count,
              target_duration=body.target_duration, sticker_sec=round(sticker_dur, 2),
              body_target=round(body_target, 2), n_combos=n_combos, n_pool=n_pool,
              n_hl=n_hl, n_hk=n_hk,
              reachable_min=round(combo_durs[0], 1) if combo_durs else None,
              reachable_max=round(combo_durs[-1], 1) if combo_durs else None,
              planned_body_sec=[round(d, 1) for _, d in combo_plan] or None,
              fallback=not combo_plan)
    if not combo_plan:
        raise HTTPException(400, _no_combo_reason(body.mode, n_hk))

    # 建批次目录放在组合检查之后：报 400 的请求不该在导出目录里留下空批次文件夹
    batch_dir = _get_batch_dir(pid, project.title, project.export_path)
    for i in range(body.count):
        pairs = combo_plan[i][0]
        plan = _plan_export(body.mode, pairs, parts_ordered)
        anchor_part_id = pairs[0][0]["part_id"]

        # 原子自增素材编号：用 SQL 层 col = col + 1，不读现有值再算，避免
        # 并发请求下数出重复编号（跟 _get_batch_dir 的计数竞态是同一类问题）。
        await db.execute(
            update(AdmakeProject).where(AdmakeProject.id == pid)
            .values(export_seq=AdmakeProject.export_seq + 1)
        )
        await db.refresh(project)
        filename_stem = _build_export_filename(project.title, project.editor_alias, project.export_seq)

        export = AdmakeExport(
            part_id=anchor_part_id,
            project_id=pid,
            mode=body.mode,
            status="pending",
            target_duration=body.target_duration,
        )
        db.add(export)
        batch_info = {"index": i + 1, "count": body.count, "n_hl": n_hl, "n_hk": n_hk,
                       "n_combos": n_combos, "n_pool": n_pool, "part_names": part_names}
        exports_created.append((export, plan, batch_info, filename_stem))

    await db.flush()
    for export, plan, batch_info, filename_stem in exports_created:
        await db.refresh(export)
        background_tasks.add_task(
            _run_export_multi,
            export.id, plan, project.id, project.title,
            body.target_duration, body.mode, filename_stem, batch_dir,
            submitted_at, batch_info, body.template_id, sticker_dur,
        )

    return [AdmakeExportOut.model_validate(e) for e, _, _, _ in exports_created]


@router.get("/projects/{pid}/exports", response_model=list[AdmakeExportOut])
async def list_project_exports(
    pid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_project(pid, user, db)
    res = await db.execute(
        select(AdmakeExport)
        .where(AdmakeExport.project_id == pid)
        .order_by(AdmakeExport.created_at.desc())
    )
    return [AdmakeExportOut.model_validate(e) for e in res.scalars().all()]


# ── AI Analysis endpoints (2.0.0.10) ─────────────────────────────────────────

@router.post("/projects/{pid}/ai-analyze", status_code=202)
async def start_ai_analyze(
    pid: int,
    background_tasks: BackgroundTasks,
    only_unanalyzed: bool = Query(False),
    part_id: int | None = Query(None),  # 只重析这一个 Part（列表项「↻ 重析」入口）
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 重入拦截：同一项目并发跑两个分析批次会互相覆盖 _ai_tasks 状态、重复计费，
    # 且两个批次各持一把 db_write_lock，SQLite 写入不再互斥
    if _ai_tasks.get(pid, {}).get("status") == "processing":
        raise HTTPException(409, "AI 分析已在进行中，请等待当前任务完成")
    project = await _get_project(pid, user, db)
    ai_model = getattr(project, "ai_model", "gemini-3.5-flash") or "gemini-3.5-flash"
    await _precheck_ai_credentials(ai_model)
    res = await db.execute(
        select(AdmakePart)
        .where(AdmakePart.project_id == pid, AdmakePart.status == "ready")
        .order_by(AdmakePart.part_index)
    )
    parts = list(res.scalars().all())
    if not parts:
        raise HTTPException(400, "没有就绪的 Part 视频，请先合并视频")

    if part_id is not None:
        parts = [p for p in parts if p.id == part_id]
        if not parts:
            raise HTTPException(404, "该 Part 不存在或尚未就绪")

    if only_unanalyzed:
        filtered = []
        for p in parts:
            cnt = (await db.execute(
                select(func.count(AdmakeMark.id)).where(AdmakeMark.part_id == p.id)
            )).scalar()
            if cnt == 0:
                filtered.append(p)
        parts = filtered
        if not parts:
            raise HTTPException(400, "所有 Part 视频已有时间戳，无需重新分析")

    background_tasks.add_task(_run_ai_analyze, pid, [p.id for p in parts], ai_model, project.title)
    return {"project_id": pid, "total": len(parts), "status": "processing", "ai_model": ai_model}


@router.get("/projects/{pid}/ai-analyze/status")
async def get_ai_analyze_status(
    pid: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_project(pid, user, db)
    task = _ai_tasks.get(pid)
    if not task:
        return {"status": "idle"}
    out = dict(task)
    # 读取时实时计算各活跃 Part 当前阶段已持续秒数（阶段可能几分钟不更新快照）
    now = _time.time()
    out["active_parts"] = [
        {"name": p["name"], "stage": p["stage"],
         "stage_elapsed": max(0, int(now - p.get("stage_since_ts", now)))}
        for p in task.get("active_parts", [])
    ]
    return out

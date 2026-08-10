import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.user import User
from app.models.project import Project
from app.models.episode import Episode, TimelineClip
from app.models.storyboard import StoryboardScene, StoryboardRef
from app.schemas.episode import (
    EpisodeCreate, EpisodeUpdate, EpisodeOut, ClipOut,
    CutRequest, MergeRequest, ExportStoryboardRequest,
)
from app.schemas.storyboard import StoryboardSceneOut, StoryboardRefOut
from app.routers.storyboard import _scene_out
from app.core.deps import get_current_user
from app.services import storage, video as vid_svc
from app.core.config import settings

router = APIRouter(tags=["剧集"])


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_episode_owned(episode_id: int, user: User, db: AsyncSession) -> Episode:
    res = await db.execute(
        select(Episode)
        .join(Project, Project.id == Episode.project_id)
        .where(Episode.id == episode_id, Project.user_id == user.id)
    )
    ep = res.scalar_one_or_none()
    if not ep:
        raise HTTPException(404, "剧集不存在或无权限")
    return ep


def _clip_to_out(c: TimelineClip) -> ClipOut:
    return ClipOut(id=c.id, label=c.label, start_pct=c.start_pct,
                   end_pct=c.end_pct, color=c.color, order=c.order)


def _enrich_episode(ep: Episode, db_scene_count: int = 0) -> EpisodeOut:
    out = EpisodeOut.model_validate(ep)
    out.scene_count = db_scene_count
    return out


def _pct_to_timestamp(pct: float, total_sec: float) -> str:
    ms = round(total_sec * pct / 100 * 1000)
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000;   ms %= 60000
    s = ms // 1000;    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ── Episodes CRUD ─────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/episodes", response_model=list[EpisodeOut])
async def list_episodes(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proj_res = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user.id)
    )
    if not proj_res.scalar_one_or_none():
        raise HTTPException(403, "项目不存在或无权限")
    res = await db.execute(
        select(Episode).where(Episode.project_id == project_id).order_by(Episode.created_at)
    )
    episodes = res.scalars().all()
    out = []
    for ep in episodes:
        cnt = (await db.execute(
            select(func.count()).where(StoryboardScene.episode_id == ep.id)
        )).scalar_one()
        out.append(_enrich_episode(ep, cnt))
    return out


@router.post("/projects/{project_id}/episodes", response_model=EpisodeOut, status_code=201)
async def create_episode(
    project_id: int,
    body: EpisodeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proj_res = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user.id)
    )
    if not proj_res.scalar_one_or_none():
        raise HTTPException(403, "项目不存在或无权限")
    ep = Episode(project_id=project_id, name=body.name)
    db.add(ep)
    await db.flush()
    return _enrich_episode(ep, 0)


@router.get("/episodes/{episode_id}", response_model=EpisodeOut)
async def get_episode(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)
    cnt = (await db.execute(
        select(func.count()).where(StoryboardScene.episode_id == ep.id)
    )).scalar_one()
    return _enrich_episode(ep, cnt)


@router.get("/episodes/{episode_id}/fps")
async def get_episode_fps(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return frame rate of the episode's source video.
    Used by the frontend to calculate per-frame seek step for ← → keys.
    """
    ep = await _get_episode_owned(episode_id, current_user, db)
    if not ep.video_path:
        return {"fps": 30.0, "frame_step": round(1 / 30.0, 6)}
    try:
        fps = await vid_svc.get_fps(ep.video_path)
    except Exception:
        fps = 30.0
    return {"fps": fps, "frame_step": round(1 / fps, 6)}


@router.put("/episodes/{episode_id}", response_model=EpisodeOut)
async def update_episode(
    episode_id: int,
    body: EpisodeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(ep, k, v)
    cnt = (await db.execute(
        select(func.count()).where(StoryboardScene.episode_id == ep.id)
    )).scalar_one()
    return _enrich_episode(ep, cnt)


@router.delete("/episodes/{episode_id}", status_code=204)
async def delete_episode(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)
    await db.delete(ep)


# ── Video Upload ──────────────────────────────────────────────────────────────

@router.post("/episodes/{episode_id}/upload", response_model=EpisodeOut)
async def upload_video(
    episode_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)
    # Get project for directory naming
    proj_res = await db.execute(select(Project).where(Project.id == ep.project_id))
    project = proj_res.scalar_one()
    proj_dir = storage.get_project_dir(project.id, project.title)
    try:
        rel_path = await storage.save_video(file, project_dir=proj_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))

    ep.video_path = rel_path
    ep.status = "草稿"
    await db.flush()

    # Get duration in background so upload returns fast
    async def _update_duration():
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as sess:
            try:
                dur = await vid_svc.get_duration(rel_path)
                ep_reload = (await sess.execute(
                    select(Episode).where(Episode.id == episode_id)
                )).scalar_one()
                ep_reload.total_duration_sec = dur
                h = int(dur // 3600); m = int((dur % 3600) // 60); s = int(dur % 60)
                ep_reload.duration = f"{h}时{m}分{s}秒" if h else f"{m}分{s:02d}秒"

                # Delete existing clips and create one default clip (0–100%)
                existing = (await sess.execute(
                    select(TimelineClip).where(TimelineClip.episode_id == episode_id)
                )).scalars().all()
                for c in existing:
                    await sess.delete(c)
                await sess.flush()

                sess.add(TimelineClip(
                    episode_id=episode_id, label="SC-01",
                    start_pct=0.0, end_pct=100.0, color=1, order=0,
                ))
                await sess.commit()
            except Exception as e:
                print(f"[_update_duration] error: {e}")
                await sess.rollback()

    background_tasks.add_task(_update_duration)

    cnt = (await db.execute(select(func.count()).where(StoryboardScene.episode_id == ep.id))).scalar_one()
    return _enrich_episode(ep, cnt)


# ── Timeline Clips ────────────────────────────────────────────────────────────

@router.post("/episodes/{episode_id}/clips/initialize", response_model=list[ClipOut])
async def initialize_clips(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create the default SC-01 clip (0-100%) if no clips exist yet."""
    ep = await _get_episode_owned(episode_id, current_user, db)
    res = await db.execute(
        select(TimelineClip).where(TimelineClip.episode_id == episode_id)
    )
    existing = res.scalars().all()
    if not existing:
        clip = TimelineClip(
            episode_id=episode_id, label="SC-01",
            start_pct=0.0, end_pct=100.0, color=1, order=0,
        )
        db.add(clip)
        await db.flush()
        existing = [clip]
    return [_clip_to_out(c) for c in existing]


@router.get("/episodes/{episode_id}/clips", response_model=list[ClipOut])
async def get_clips(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)
    res = await db.execute(
        select(TimelineClip).where(TimelineClip.episode_id == ep.id).order_by(TimelineClip.order)
    )
    return [_clip_to_out(c) for c in res.scalars().all()]


@router.post("/episodes/{episode_id}/clips/cut", response_model=list[ClipOut])
async def cut_clip(
    episode_id: int,
    body: CutRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)
    res = await db.execute(
        select(TimelineClip).where(TimelineClip.episode_id == ep.id).order_by(TimelineClip.order)
    )
    clips = list(res.scalars().all())

    pct = body.position_pct
    target = next((c for c in clips if c.start_pct + 0.1 < pct < c.end_pct - 0.1), None)
    if not target:
        raise HTTPException(400, "游标不在任何片段内部，无法裁切")

    color_a = target.color
    color_b = (color_a % 4) + 1
    base_label = target.label.rstrip("abcdefgh")

    # Insert two new clips, remove old
    idx = clips.index(target)
    await db.delete(target)
    await db.flush()

    clip_a = TimelineClip(episode_id=ep.id, label=base_label + "a",
                          start_pct=target.start_pct, end_pct=pct, color=color_a, order=idx)
    clip_b = TimelineClip(episode_id=ep.id, label=base_label + "b",
                          start_pct=pct, end_pct=target.end_pct, color=color_b, order=idx + 1)
    db.add_all([clip_a, clip_b])

    # Re-order remaining clips
    for i, c in enumerate(clips[idx + 1:], start=idx + 2):
        c.order = i

    await db.flush()
    res2 = await db.execute(
        select(TimelineClip).where(TimelineClip.episode_id == ep.id).order_by(TimelineClip.order)
    )
    return [_clip_to_out(c) for c in res2.scalars().all()]


@router.post("/episodes/{episode_id}/clips/merge", response_model=list[ClipOut])
async def merge_clips(
    episode_id: int,
    body: MergeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)
    res = await db.execute(
        select(TimelineClip).where(TimelineClip.episode_id == ep.id).order_by(TimelineClip.order)
    )
    all_clips = list(res.scalars().all())
    sel = sorted([c for c in all_clips if c.id in body.clip_ids], key=lambda c: c.start_pct)

    if len(sel) < 2:
        raise HTTPException(400, "至少选择 2 个片段")
    for i in range(1, len(sel)):
        if abs(sel[i].start_pct - sel[i - 1].end_pct) > 0.5:
            raise HTTPException(400, "只能合并连续相邻片段")

    base_label = sel[0].label.rstrip("abcdefgh")
    merged_idx = all_clips.index(sel[0])
    merged = TimelineClip(
        episode_id=ep.id, label=base_label,
        start_pct=sel[0].start_pct, end_pct=sel[-1].end_pct,
        color=sel[0].color, order=merged_idx,
    )
    for c in sel:
        await db.delete(c)
    db.add(merged)

    remaining = [c for c in all_clips if c not in sel]
    for i, c in enumerate(remaining[merged_idx:], start=merged_idx + 1):
        c.order = i

    await db.flush()
    res2 = await db.execute(
        select(TimelineClip).where(TimelineClip.episode_id == ep.id).order_by(TimelineClip.order)
    )
    return [_clip_to_out(c) for c in res2.scalars().all()]


# ── Export to Storyboard ──────────────────────────────────────────────────────

@router.post("/episodes/{episode_id}/export-storyboard", response_model=list[StoryboardSceneOut])
async def export_storyboard(
    episode_id: int,
    body: ExportStoryboardRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ep = await _get_episode_owned(episode_id, current_user, db)

    # Get project for directory naming
    proj_res = await db.execute(select(Project).where(Project.id == ep.project_id))
    project = proj_res.scalar_one()

    clips_res = await db.execute(
        select(TimelineClip).where(TimelineClip.episode_id == ep.id).order_by(TimelineClip.order)
    )
    clips = list(clips_res.scalars().all())

    existing_res = await db.execute(
        select(StoryboardScene).where(StoryboardScene.episode_id == ep.id).order_by(StoryboardScene.order)
    )
    existing_scenes = {s.clip_id: s for s in existing_res.scalars().all()}

    # 删除不再对应任何 clip 的旧分镜（clip 被合并/删除的情况）
    current_clip_ids = {c.id for c in clips}
    for clip_id, scene in list(existing_scenes.items()):
        if clip_id not in current_clip_ids:
            await db.delete(scene)
            del existing_scenes[clip_id]

    # 准备 FFmpeg 输出目录
    proj_dir  = storage.get_project_dir(project.id, project.title)
    ep_slug   = storage.slugify(ep.name)
    clips_rel = os.path.join("DramaCreate", "clips", proj_dir, ep_slug)
    clips_abs = os.path.join(settings.UPLOAD_DIR, clips_rel)
    os.makedirs(clips_abs, exist_ok=True)

    scenes_to_return: list[StoryboardScene] = []

    for i, clip in enumerate(clips):
        dur   = _pct_to_timestamp(clip.end_pct - clip.start_pct, ep.total_duration_sec)
        label = f"分镜{i+1}"

        scene = existing_scenes.get(clip.id)

        # 判断裁切点是否有变化（误差 < 0.001% 视为未变）
        TOLERANCE = 0.001
        clip_changed = (
            scene is None
            or scene.exported_start is None
            or scene.exported_end   is None
            or abs(scene.exported_start - clip.start_pct) > TOLERANCE
            or abs(scene.exported_end   - clip.end_pct)   > TOLERANCE
        )

        if scene is None:
            # 新片段，创建分镜
            scene = StoryboardScene(
                episode_id=ep.id, clip_id=clip.id,
                scene_label=label, order=i, duration=dur,
            )
            db.add(scene)
        else:
            # 更新标签和顺序（防止合并/排序后标签错乱）
            scene.scene_label = label
            scene.order       = i
            scene.duration    = dur

        if clip_changed and ep.video_path and ep.total_duration_sec > 0:
            # 裁切点有变化 → 重新裁切
            start_sec = ep.total_duration_sec * clip.start_pct / 100
            end_sec   = ep.total_duration_sec * clip.end_pct   / 100
            out_rel   = os.path.join(clips_rel, f"分镜_{i+1}.mp4")
            out_abs   = os.path.join(settings.UPLOAD_DIR, out_rel)
            try:
                await vid_svc.cut_video_copy(ep.video_path, start_sec, end_sec, out_abs)
                scene.clip_path       = out_rel   # 更新原始裁切片段
                scene.exported_start  = clip.start_pct
                scene.exported_end    = clip.end_pct
                clip.clip_video_path  = out_rel
                # 若之前已有 AI 生成视频，保留 video_path 但将状态改回草稿
                if scene.video_path:
                    scene.status = "草稿"
                print(f"[export_storyboard] {label} re-cut OK: {out_rel}")
            except Exception as e:
                import traceback
                print(f"[export_storyboard] {label} FFmpeg FAILED: {e}")
                traceback.print_exc()
                raise HTTPException(500, f"{label} 裁切失败: {e}")
        else:
            if not clip_changed:
                print(f"[export_storyboard] {label} unchanged, skipped FFmpeg ✓")

        scenes_to_return.append(scene)

    await db.flush()

    out = []
    for s in scenes_to_return:
        refs_res = await db.execute(
            select(StoryboardRef).where(StoryboardRef.scene_id == s.id)
        )
        out.append(_scene_out(s, list(refs_res.scalars().all())))
    return out


# ── Export Final Video (synchronous, H.264 CRF=20, AAC 128k, 1080x1920) ─────

@router.post("/episodes/{episode_id}/export-video")
async def export_video(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Synchronously merge all storyboard scene videos into a single output file.
    Uses full re-encode (H.264 CRF=20 + AAC 128k) for clean keyframes and timestamps.
    Resolution: 1080×1920 (portrait). Falls back to clip-cut video if no AI video.
    """
    ep = await _get_episode_owned(episode_id, current_user, db)

    # Get project for output directory
    proj_res = await db.execute(select(Project).where(Project.id == ep.project_id))
    project = proj_res.scalar_one()

    # Collect storyboard scenes in order
    scenes_res = await db.execute(
        select(StoryboardScene).where(StoryboardScene.episode_id == ep.id).order_by(StoryboardScene.order)
    )
    scenes = list(scenes_res.scalars().all())
    if not scenes:
        raise HTTPException(400, "请先导出分镜后再成片导出")

    # Collect input paths: prefer AI-generated video, fall back to clip cut
    input_paths: list[str] = []
    for s in scenes:
        if s.video_path:
            p = storage.abs_path(s.video_path)
            if os.path.exists(p):
                input_paths.append(s.video_path)
                continue
        # Fall back: try the linked clip
        if s.clip_id:
            clip_res = await db.execute(
                select(TimelineClip).where(TimelineClip.id == s.clip_id)
            )
            clip = clip_res.scalar_one_or_none()
            if clip and clip.clip_video_path:
                p = storage.abs_path(clip.clip_video_path)
                if os.path.exists(p):
                    input_paths.append(clip.clip_video_path)
                    continue
        # Skip scenes with no video
        print(f"[export_video] scene {s.scene_label} has no video, skipping")

    if not input_paths:
        raise HTTPException(400, "没有可导出的视频片段，请先导出分镜")

    # Build output path: exports/{proj_dir}/{ep_slug}_final.mp4
    proj_dir = storage.get_project_dir(project.id, project.title)
    ep_slug  = storage.slugify(ep.name)
    out_rel  = os.path.join("DramaCreate", "exports", proj_dir, f"{ep_slug}_final.mp4")
    out_abs  = os.path.join(settings.UPLOAD_DIR, out_rel)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)

    # Get resolution from project settings
    res_map  = {"1080P": (1080, 1920), "720P": (720, 1280), "4K": (2160, 3840)}
    w, h     = res_map.get(project.resolution, (1080, 1920))

    # Run FFmpeg synchronously (blocking — user sees fake progress bar on frontend)
    try:
        await vid_svc.export_final_video(input_paths, out_abs, width=w, height=h, crf=20)
    except Exception as e:
        raise HTTPException(500, f"导出失败: {e}")

    # Update episode status
    ep.status = "生成成功"
    await db.flush()

    return {
        "message": "导出成功",
        "output_path": out_rel,
        "episode_id": episode_id,
    }

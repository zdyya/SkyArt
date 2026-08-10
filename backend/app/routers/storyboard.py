from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.user import User
from app.models.episode import Episode
from app.models.project import Project
from app.models.asset import Character, SceneSetting, Prop, AssetImage
from app.models.storyboard import StoryboardScene, StoryboardRef
from app.schemas.storyboard import (
    StoryboardSceneOut, StoryboardSceneUpdate, StoryboardRefOut,
    GenerateVideoRequest, GenerateVideoResponse,
)
from app.core.deps import get_current_user
from app.core.config import settings
from app.services import ai_mock, happyhorse as hh_svc

router = APIRouter(tags=["分镜"])


def _scene_out(s: StoryboardScene, refs: list = []) -> StoryboardSceneOut:
    """Build StoryboardSceneOut without triggering lazy .refs load."""
    return StoryboardSceneOut(
        id=s.id, episode_id=s.episode_id, clip_id=s.clip_id,
        scene_label=s.scene_label, order=s.order, duration=s.duration,
        status=s.status, shot_type=s.shot_type, angle=s.angle,
        weather=s.weather, camera_move=s.camera_move,
        time_period=s.time_period, script=s.script,
        clip_path=s.clip_path, exported_start=s.exported_start, exported_end=s.exported_end,
        video_path=s.video_path,
        generated_videos=__import__('json').loads(s.generated_videos or '[]'),
        task_id=s.task_id,
        created_at=s.created_at, updated_at=s.updated_at,
        refs=[StoryboardRefOut(
            id=r.id, ref_type=r.ref_type,
            ref_asset_id=r.ref_asset_id, ref_name=r.ref_name,
        ) for r in refs],
    )


async def _get_scene_owned(scene_id: int, user: User, db: AsyncSession) -> StoryboardScene:
    res = await db.execute(
        select(StoryboardScene)
        .join(Episode, Episode.id == StoryboardScene.episode_id)
        .join(Project, Project.id == Episode.project_id)
        .where(StoryboardScene.id == scene_id, Project.user_id == user.id)
    )
    scene = res.scalar_one_or_none()
    if not scene:
        raise HTTPException(404, "分镜不存在或无权限")
    return scene


@router.get("/episodes/{episode_id}/storyboard", response_model=list[StoryboardSceneOut])
async def get_storyboard(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Verify ownership
    ep_res = await db.execute(
        select(Episode)
        .join(Project, Project.id == Episode.project_id)
        .where(Episode.id == episode_id, Project.user_id == user.id)
    )
    if not ep_res.scalar_one_or_none():
        raise HTTPException(403, "无权限")

    res = await db.execute(
        select(StoryboardScene)
        .where(StoryboardScene.episode_id == episode_id)
        .order_by(StoryboardScene.order)
    )
    scenes = res.scalars().all()
    out = []
    for s in scenes:
        refs_res = await db.execute(
            select(StoryboardRef).where(StoryboardRef.scene_id == s.id)
        )
        out.append(_scene_out(s, refs_res.scalars().all()))
    return out


@router.put("/storyboard/{scene_id}", response_model=StoryboardSceneOut)
async def update_storyboard_scene(
    scene_id: int,
    body: StoryboardSceneUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    scene = await _get_scene_owned(scene_id, user, db)
    # exclude_unset=True 保留显式传入的 None（如清空 video_path）
    update_data = body.model_dump(exclude_unset=True)

    # Handle refs separately
    refs_data = update_data.pop("refs", None)

    for k, v in update_data.items():
        setattr(scene, k, v)

    if refs_data is not None:
        # Delete existing refs and re-create
        existing_refs = await db.execute(select(StoryboardRef).where(StoryboardRef.scene_id == scene_id))
        for r in existing_refs.scalars().all():
            await db.delete(r)
        for ref in refs_data:
            db.add(StoryboardRef(
                scene_id=scene_id,
                ref_type=ref["ref_type"],
                ref_asset_id=ref["ref_asset_id"],
                ref_name=ref.get("ref_name", ""),
            ))

    await db.flush()
    refs_res = await db.execute(select(StoryboardRef).where(StoryboardRef.scene_id == scene_id))
    return _scene_out(scene, refs_res.scalars().all())


@router.delete("/storyboard/{scene_id}", status_code=204)
async def delete_storyboard_scene(
    scene_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    scene = await _get_scene_owned(scene_id, user, db)
    await db.delete(scene)


@router.post("/storyboard/{scene_id}/generate-video", response_model=GenerateVideoResponse)
async def generate_video(
    scene_id: int,
    body: GenerateVideoRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Generate a storyboard video using HappyHorse video-edit API.

    body.prompt: pre-packaged prompt with [video1],[image1]... placeholders
                 (frontend builds this from the @-mention description)
    body.ref_image_paths: ordered list of reference image relative paths
    body.resolution: "1080P" | "720P" (from project settings)

    Falls back to mock when HAPPYHORSE_API_KEY is not set.
    """
    scene = await _get_scene_owned(scene_id, user, db)
    refs_res = await db.execute(select(StoryboardRef).where(StoryboardRef.scene_id == scene_id))
    refs = list(refs_res.scalars().all())

    # 用 clip_path（FFmpeg 裁切的原始片段）作为源视频，不用 video_path（AI 生成视频）
    video_rel = scene.clip_path

    if not video_rel:
        raise HTTPException(400, "该分镜还没有视频切片，请先导出分镜")

    # Collect reference image paths (ordered as sent by frontend)
    ref_image_paths: list[str] = getattr(body, "ref_image_paths", None) or []

    # If frontend didn't pass explicit paths, fall back to looking up each ref's base image
    if not ref_image_paths:
        for ref in refs:
            base_img = await _get_ref_base_image(ref.ref_type, ref.ref_asset_id, db)
            if base_img:
                ref_image_paths.append(base_img)

    # Build prompt — use body.prompt if provided (frontend packaged), else use scene.script
    prompt = getattr(body, "prompt", None) or scene.script or "替换视频中的要素"

    # Get project resolution
    resolution = getattr(body, "resolution", None) or "1080P"

    # Submit to HappyHorse
    try:
        task_id = await hh_svc.submit_video_edit(
            prompt=prompt,
            video_rel_path=video_rel,
            ref_image_rel_paths=ref_image_paths,
            resolution=resolution,
        )
    except Exception as e:
        raise HTTPException(500, f"提交视频生成失败: {e}")

    # Update scene status to processing
    scene.status = "生成中"
    scene.task_id = task_id if hasattr(scene, "task_id") else None
    await db.flush()

    return GenerateVideoResponse(
        task_id=task_id,
        status="processing",
        message="视频生成任务已提交，请稍候",
    )


async def _get_ref_base_image(ref_type: str, asset_id: int, db: AsyncSession):
    """Get base image path for a character/scene/prop asset."""
    from app.models.asset import Character, SceneSetting, Prop
    if ref_type == "char":
        res = await db.execute(select(Character).where(Character.id == asset_id))
        a = res.scalar_one_or_none()
    elif ref_type == "scene":
        res = await db.execute(select(SceneSetting).where(SceneSetting.id == asset_id))
        a = res.scalar_one_or_none()
    elif ref_type == "prop":
        res = await db.execute(select(Prop).where(Prop.id == asset_id))
        a = res.scalar_one_or_none()
    else:
        return None
    return a.base_image_path if a else None


@router.get("/storyboard/task/{task_id}")
async def poll_video_task(
    task_id: str,
    scene_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Poll HappyHorse task status.
    When SUCCEEDED, downloads the video and updates the scene.
    """
    result = await hh_svc.get_task_status(task_id)
    status = result["status"]

    if status == "SUCCEEDED" and result.get("output_url"):
        # Download the generated video and save it locally
        import httpx, uuid, os
        from app.core.config import settings as cfg
        from app.models.storyboard import StoryboardScene

        # Get scene to update
        scene_res = await db.execute(
            select(StoryboardScene).where(StoryboardScene.id == scene_id)
        )
        scene = scene_res.scalar_one_or_none()
        if scene:
            try:
                out_rel = os.path.join("DramaCreate", "generated", f"{uuid.uuid4().hex}.mp4")
                out_abs = os.path.join(cfg.UPLOAD_DIR, out_rel)
                os.makedirs(os.path.dirname(out_abs), exist_ok=True)
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.get(result["output_url"])
                    resp.raise_for_status()
                    with open(out_abs, "wb") as f:
                        f.write(resp.content)
                import json as _json
                # 累积保存所有生成视频，不覆盖旧的
                existing = _json.loads(scene.generated_videos or '[]')
                existing.append(out_rel)
                scene.generated_videos = _json.dumps(existing, ensure_ascii=False)
                scene.video_path = out_rel   # 最新生成的设为当前选定
                scene.status = "已完成"
                await db.flush()
                result["local_path"] = out_rel
            except Exception as e:
                result["download_error"] = str(e)

    return result

"""Characters, SceneSettings, Props — unified asset management."""
import json
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.user import User
from app.models.project import Project
from app.models.asset import Character, SceneSetting, Prop, AssetImage
from app.schemas.asset import (
    CharacterCreate, CharacterUpdate, CharacterOut,
    SceneSettingCreate, SceneSettingUpdate, SceneSettingOut,
    PropCreate, PropUpdate, PropOut,
    AssetImageOut, GenerateImageRequest, GenerateImageResponse,
)
from app.core.deps import get_current_user
from app.services import storage, ai_mock

router = APIRouter(tags=["资产"])


def _char_out(char: "Character", images: list = []) -> "CharacterOut":
    """Build CharacterOut without triggering lazy relationship load."""
    return CharacterOut(
        id=char.id, project_id=char.project_id, name=char.name,
        gender=char.gender, age=char.age or '', appearance=char.appearance,
        personality=char.personality, ethnicity=char.ethnicity,
        status=char.status, base_image_path=char.base_image_path,
        created_at=char.created_at, images=[AssetImageOut.model_validate(i) for i in images],
    )


def _prop_out(prop: "Prop") -> "PropOut":
    """Build PropOut without corrupting the ORM object.

    PropOut.model_validate() writes parsed aliases back into prop.__dict__,
    turning the Text column value from str to list.  When get_db later calls
    session.commit() SQLAlchemy detects the 'dirty' change and emits a
    spurious UPDATE (str → list), which fails on SQLite and rolls back the
    entire transaction — causing every prop create/update to silently revert.
    """
    try:
        aliases = json.loads(prop.aliases) if isinstance(prop.aliases, str) else (prop.aliases or [])
    except Exception:
        aliases = []
    return PropOut(
        id=prop.id, project_id=prop.project_id, name=prop.name,
        description=prop.description, aliases=aliases, status=prop.status,
        base_image_path=prop.base_image_path, created_at=prop.created_at,
    )


async def _check_project(project_id: int, user: User, db: AsyncSession) -> Project:
    res = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    proj = res.scalar_one_or_none()
    if not proj:
        raise HTTPException(403, "项目不存在或无权限")
    return proj


# ══════════════════════════════════════════════════════════
#  Characters
# ══════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/characters", response_model=list[CharacterOut])
async def list_characters(project_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    await _check_project(project_id, user, db)
    res = await db.execute(select(Character).where(Character.project_id == project_id))
    chars = res.scalars().all()
    out = []
    for c in chars:
        imgs_res = await db.execute(
            select(AssetImage).where(AssetImage.asset_type == "character", AssetImage.asset_id == c.id)
        )
        out.append(_char_out(c, imgs_res.scalars().all()))
    return out


@router.post("/projects/{project_id}/characters", response_model=CharacterOut, status_code=201)
async def create_character(
    project_id: int, body: CharacterCreate,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    await _check_project(project_id, user, db)
    char = Character(**body.model_dump(), project_id=project_id)
    db.add(char)
    await db.flush()
    return _char_out(char, [])


@router.put("/characters/{char_id}", response_model=CharacterOut)
async def update_character(
    char_id: int, body: CharacterUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    res = await db.execute(select(Character).where(Character.id == char_id))
    char = res.scalar_one_or_none()
    if not char:
        raise HTTPException(404, "角色不存在")
    await _check_project(char.project_id, user, db)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(char, k, v)
    imgs_res = await db.execute(
        select(AssetImage).where(AssetImage.asset_type == "character", AssetImage.asset_id == char.id)
    )
    return _char_out(char, imgs_res.scalars().all())


@router.delete("/characters/{char_id}", status_code=204)
async def delete_character(char_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    res = await db.execute(select(Character).where(Character.id == char_id))
    char = res.scalar_one_or_none()
    if not char:
        raise HTTPException(404, "角色不存在")
    await _check_project(char.project_id, user, db)
    await db.delete(char)


@router.post("/characters/{char_id}/generate-image", response_model=GenerateImageResponse)
async def generate_character_image(
    char_id: int, body: GenerateImageRequest,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    res = await db.execute(select(Character).where(Character.id == char_id))
    char = res.scalar_one_or_none()
    if not char:
        raise HTTPException(404, "角色不存在")
    await _check_project(char.project_id, user, db)
    prompt = f"{char.name} {char.appearance or ''}"
    paths = await ai_mock.generate_image("character", char_id, prompt, body.count, body.model_api)
    for p in paths:
        db.add(AssetImage(asset_type="character", asset_id=char_id, image_path=p))
    if not char.base_image_path and paths:
        char.base_image_path = paths[0]
    await db.flush()
    return GenerateImageResponse(task_id=f"char-{char_id}", status="completed",
                                 message=f"已生成 {len(paths)} 张图片")


@router.post("/characters/{char_id}/set-base-image")
async def set_base_image(
    char_id: int, image_id: int,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    res = await db.execute(select(Character).where(Character.id == char_id))
    char = res.scalar_one_or_none()
    if not char:
        raise HTTPException(404, "角色不存在")
    await _check_project(char.project_id, user, db)
    imgs = await db.execute(
        select(AssetImage).where(AssetImage.asset_type == "character", AssetImage.asset_id == char_id)
    )
    for img in imgs.scalars().all():
        img.is_base = img.id == image_id
        if img.is_base:
            char.base_image_path = img.image_path
    return {"ok": True}


# ══════════════════════════════════════════════════════════
#  SceneSettings
# ══════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/scenes", response_model=list[SceneSettingOut])
async def list_scenes(project_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    await _check_project(project_id, user, db)
    res = await db.execute(select(SceneSetting).where(SceneSetting.project_id == project_id))
    return [SceneSettingOut.model_validate(s) for s in res.scalars().all()]


@router.post("/projects/{project_id}/scenes", response_model=SceneSettingOut, status_code=201)
async def create_scene(
    project_id: int, body: SceneSettingCreate,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    await _check_project(project_id, user, db)
    scene = SceneSetting(**body.model_dump(), project_id=project_id)
    db.add(scene)
    await db.flush()
    return SceneSettingOut.model_validate(scene)


@router.put("/scenes/{scene_id}", response_model=SceneSettingOut)
async def update_scene(
    scene_id: int, body: SceneSettingUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    res = await db.execute(select(SceneSetting).where(SceneSetting.id == scene_id))
    scene = res.scalar_one_or_none()
    if not scene:
        raise HTTPException(404, "场景不存在")
    await _check_project(scene.project_id, user, db)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(scene, k, v)
    return SceneSettingOut.model_validate(scene)


@router.delete("/scenes/{scene_id}", status_code=204)
async def delete_scene(scene_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    res = await db.execute(select(SceneSetting).where(SceneSetting.id == scene_id))
    scene = res.scalar_one_or_none()
    if not scene:
        raise HTTPException(404, "场景不存在")
    await _check_project(scene.project_id, user, db)
    await db.delete(scene)


@router.post("/scenes/{scene_id}/generate-image", response_model=GenerateImageResponse)
async def generate_scene_image(
    scene_id: int, body: GenerateImageRequest,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    res = await db.execute(select(SceneSetting).where(SceneSetting.id == scene_id))
    scene = res.scalar_one_or_none()
    if not scene:
        raise HTTPException(404, "场景不存在")
    await _check_project(scene.project_id, user, db)
    paths = await ai_mock.generate_image("scene", scene_id, scene.description or scene.name, body.count)
    for p in paths:
        db.add(AssetImage(asset_type="scene", asset_id=scene_id, image_path=p))
    if not scene.base_image_path and paths:
        scene.base_image_path = paths[0]
    await db.flush()
    return GenerateImageResponse(task_id=f"scene-{scene_id}", status="completed",
                                 message=f"已生成 {len(paths)} 张图片")


# ══════════════════════════════════════════════════════════
#  Props
# ══════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/props", response_model=list[PropOut])
async def list_props(project_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    await _check_project(project_id, user, db)
    res = await db.execute(select(Prop).where(Prop.project_id == project_id))
    props = res.scalars().all()
    return [_prop_out(p) for p in props]


@router.post("/projects/{project_id}/props", response_model=PropOut, status_code=201)
async def create_prop(
    project_id: int, body: PropCreate,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    await _check_project(project_id, user, db)
    data = body.model_dump()
    data["aliases"] = json.dumps(data.get("aliases", []), ensure_ascii=False)
    prop = Prop(**data, project_id=project_id)
    db.add(prop)
    await db.flush()
    return _prop_out(prop)


@router.put("/props/{prop_id}", response_model=PropOut)
async def update_prop(
    prop_id: int, body: PropUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    res = await db.execute(select(Prop).where(Prop.id == prop_id))
    prop = res.scalar_one_or_none()
    if not prop:
        raise HTTPException(404, "道具不存在")
    await _check_project(prop.project_id, user, db)
    data = body.model_dump(exclude_none=True)
    if "aliases" in data:
        data["aliases"] = json.dumps(data["aliases"], ensure_ascii=False)
    for k, v in data.items():
        setattr(prop, k, v)
    return _prop_out(prop)


@router.delete("/props/{prop_id}", status_code=204)
async def delete_prop(prop_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    res = await db.execute(select(Prop).where(Prop.id == prop_id))
    prop = res.scalar_one_or_none()
    if not prop:
        raise HTTPException(404, "道具不存在")
    await _check_project(prop.project_id, user, db)
    await db.delete(prop)


# ── Image upload (generic) ────────────────────────────────────────────────────

@router.post("/upload/image")
async def upload_image(
    file: UploadFile = File(...),
    _user: User = Depends(get_current_user),
):
    try:
        rel = await storage.save_image(file)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"path": rel, "url": storage.url_path(rel)}


# ── Upload images for character / scene / prop (replaces AI generate) ─────────

async def _upload_asset_images(asset_type: str, asset_id: int, project_id: int,
                                files: list[UploadFile], user: User, db: AsyncSession):
    await _check_project(project_id, user, db)
    saved = []
    for f in files:
        try:
            rel = await storage.save_image(f)
            img = AssetImage(asset_type=asset_type, asset_id=asset_id, image_path=rel)
            db.add(img)
            saved.append(rel)
        except ValueError:
            pass
    await db.flush()
    return saved


@router.post("/characters/{char_id}/upload-images")
async def upload_character_images(
    char_id: int,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(select(Character).where(Character.id == char_id))
    char = res.scalar_one_or_none()
    if not char:
        raise HTTPException(404, "角色不存在")
    paths = await _upload_asset_images("character", char_id, char.project_id, files, user, db)
    if not char.base_image_path and paths:
        char.base_image_path = paths[0]
    return {"uploaded": len(paths), "paths": paths}


@router.post("/scenes/{scene_id}/upload-images")
async def upload_scene_images(
    scene_id: int,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(select(SceneSetting).where(SceneSetting.id == scene_id))
    scene = res.scalar_one_or_none()
    if not scene:
        raise HTTPException(404, "场景不存在")
    paths = await _upload_asset_images("scene", scene_id, scene.project_id, files, user, db)
    if not scene.base_image_path and paths:
        scene.base_image_path = paths[0]
    return {"uploaded": len(paths), "paths": paths}


@router.post("/props/{prop_id}/upload-images")
async def upload_prop_images(
    prop_id: int,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(select(Prop).where(Prop.id == prop_id))
    prop = res.scalar_one_or_none()
    if not prop:
        raise HTTPException(404, "道具不存在")
    paths = await _upload_asset_images("prop", prop_id, prop.project_id, files, user, db)
    if not prop.base_image_path and paths:
        prop.base_image_path = paths[0]
    return {"uploaded": len(paths), "paths": paths}

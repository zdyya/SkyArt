from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.user import User
from app.models.project import Project
from app.models.episode import Episode
from app.schemas.project import ProjectCreate, ProjectUpdate, ProjectOut
from app.core.deps import get_current_user

router = APIRouter(prefix="/projects", tags=["项目"])


def _check_owner(project: Project, user: User):
    if project.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权限访问该项目")


async def _enrich(project: Project, db: AsyncSession) -> ProjectOut:
    count_res = await db.execute(
        select(func.count()).where(Episode.project_id == project.id)
    )
    count = count_res.scalar_one()
    out = ProjectOut.model_validate(project)
    out.episode_count = count
    return out


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(Project)
        .where(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
    )
    projects = res.scalars().all()
    return [await _enrich(p, db) for p in projects]


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = Project(**body.model_dump(), user_id=current_user.id)
    db.add(project)
    await db.flush()
    return await _enrich(project, db)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(select(Project).where(Project.id == project_id))
    project = res.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    _check_owner(project, current_user)
    return await _enrich(project, db)


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(select(Project).where(Project.id == project_id))
    project = res.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    _check_owner(project, current_user)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(project, field, value)
    return await _enrich(project, db)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(select(Project).where(Project.id == project_id))
    project = res.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    _check_owner(project, current_user)
    await db.delete(project)

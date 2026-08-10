from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class EpisodeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class EpisodeUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None


class EpisodeOut(BaseModel):
    id: int
    project_id: int
    name: str
    status: str
    duration: str
    total_duration_sec: float
    video_path: Optional[str]
    scene_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Timeline Clips ────────────────────────────────────────────────────────────

class ClipOut(BaseModel):
    id: int
    label: str
    start: float = Field(..., alias="start_pct")
    end: float = Field(..., alias="end_pct")
    color: int
    order: int

    model_config = {"from_attributes": True, "populate_by_name": True}


class CutRequest(BaseModel):
    position_pct: float = Field(..., ge=0, le=100, description="游标位置（0-100%）")


class MergeRequest(BaseModel):
    clip_ids: list[int] = Field(..., min_length=2)


class ExportStoryboardRequest(BaseModel):
    """从前端传来的当前分镜列表，用于增量同步。"""
    incremental: bool = False  # False=全量替换, True=只增量更新

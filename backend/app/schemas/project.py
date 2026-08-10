from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class ProjectCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    project_type: str = "短剧"
    platform: str = "TikTok"
    resolution: str = "1080P"
    drama_style: str = "古装"
    art_style: Optional[str] = None
    prompt_template: str = "默认模板"


class ProjectUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    project_type: Optional[str] = None
    platform: Optional[str] = None
    resolution: Optional[str] = None
    drama_style: Optional[str] = None
    art_style: Optional[str] = None
    prompt_template: Optional[str] = None


class ProjectOut(BaseModel):
    id: int
    title: str
    description: Optional[str]
    project_type: str
    platform: str
    resolution: str
    drama_style: str
    art_style: Optional[str]
    prompt_template: str
    episode_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

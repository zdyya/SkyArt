from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional
import json


# ── Character ─────────────────────────────────────────────────────────────────

class CharacterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    gender: str = "男"
    age: str = ""
    appearance: Optional[str] = None
    personality: Optional[str] = None
    ethnicity: Optional[str] = None
    status: str = "草稿"


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None
    appearance: Optional[str] = None
    personality: Optional[str] = None
    ethnicity: Optional[str] = None
    status: Optional[str] = None


class AssetImageOut(BaseModel):
    id: int
    image_path: str
    is_base: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CharacterOut(BaseModel):
    id: int
    project_id: int
    name: str
    gender: str
    age: str
    appearance: Optional[str]
    personality: Optional[str]
    ethnicity: Optional[str]
    status: str
    base_image_path: Optional[str]
    images: list[AssetImageOut] = []
    created_at: datetime

    model_config = {"from_attributes": True}


# ── SceneSetting ──────────────────────────────────────────────────────────────

class SceneSettingCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    time_of_day: str = "白天"
    weather: str = "晴天"
    status: str = "草稿"


class SceneSettingUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    time_of_day: Optional[str] = None
    weather: Optional[str] = None
    status: Optional[str] = None


class SceneSettingOut(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str]
    time_of_day: str
    weather: str
    status: str
    base_image_path: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Prop ──────────────────────────────────────────────────────────────────────

class PropCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    aliases: list[str] = []
    status: str = "草稿"


class PropUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    aliases: Optional[list[str]] = None
    status: Optional[str] = None


class PropOut(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str]
    aliases: list[str] = []
    status: str
    base_image_path: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def model_validate(cls, obj, **kwargs):
        if hasattr(obj, "aliases") and isinstance(obj.aliases, str):
            try:
                obj.__dict__["aliases"] = json.loads(obj.aliases)
            except Exception:
                obj.__dict__["aliases"] = []
        return super().model_validate(obj, **kwargs)


# ── AI Generation ─────────────────────────────────────────────────────────────

class GenerateImageRequest(BaseModel):
    model_api: str = "QWen"
    resolution: str = "1080P"
    count: int = Field(4, ge=1, le=8)


class GenerateImageResponse(BaseModel):
    task_id: str
    status: str = "processing"
    message: str = "图像生成任务已提交"

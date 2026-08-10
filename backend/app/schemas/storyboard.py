from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class StoryboardRefOut(BaseModel):
    id: int
    ref_type: str
    ref_asset_id: int
    ref_name: str

    model_config = {"from_attributes": True}


class StoryboardSceneOut(BaseModel):
    id: int
    episode_id: int
    clip_id: Optional[int]
    scene_label: str
    order: int
    duration: str
    status: str
    shot_type: str
    angle: str
    weather: str
    camera_move: str
    time_period: str
    script: str
    clip_path: Optional[str] = None       # FFmpeg 原始裁切片段
    exported_start: Optional[float] = None
    exported_end:   Optional[float] = None
    video_path: Optional[str] = None      # 当前选定视频
    generated_videos: list[str] = []      # 所有AI生成视频（累积）
    task_id: Optional[str] = None
    refs: list[StoryboardRefOut] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StoryboardSceneUpdate(BaseModel):
    duration: Optional[str] = None
    status: Optional[str] = None
    shot_type: Optional[str] = None
    angle: Optional[str] = None
    weather: Optional[str] = None
    camera_move: Optional[str] = None
    time_period: Optional[str] = None
    script: Optional[str] = None
    video_path: Optional[str] = None   # 切换选定视频
    # Refs: list of {ref_type, ref_asset_id, ref_name}
    refs: Optional[list[dict]] = None


class GenerateVideoRequest(BaseModel):
    model_api: str = "HappyHorse"
    resolution: str = "1080P"
    # Packaged prompt from frontend (with [video1],[image1]... placeholders)
    prompt: Optional[str] = None
    # Ordered list of reference image relative paths (matching the [image1],[image2]... order)
    ref_image_paths: Optional[list[str]] = None


class GenerateVideoResponse(BaseModel):
    task_id: str
    status: str = "processing"
    message: str = "视频生成任务已提交"

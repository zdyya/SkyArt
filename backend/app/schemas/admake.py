from pydantic import BaseModel, computed_field
from datetime import datetime


class AdmakeProjectCreate(BaseModel):
    title: str
    description: str | None = None
    prompt_template: str = "默认模板"
    target_platform: str = "TikTok"
    ai_model: str = "gemini-3.5-flash"
    editor_alias: str = ""
    drama_project_id: int | None = None
    export_path: str | None = None


class AdmakeProjectUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    prompt_template: str | None = None
    target_platform: str | None = None
    ai_model: str | None = None
    editor_alias: str | None = None
    default_template_id: int | None = None
    # 空字符串 = 清空自定义导出目录，回到默认目录
    export_path: str | None = None


class AdmakeSourceOut(BaseModel):
    id: int
    original_filename: str
    file_path: str
    duration_sec: float
    head_trim: float = 0.0
    tail_trim: float = 0.0
    sort_order: int
    created_at: datetime
    model_config = {"from_attributes": True}


class AdmakePartOut(BaseModel):
    id: int
    name: str
    part_index: int
    file_path: str | None
    duration_sec: float
    status: str
    ai_status: str = "none"
    ai_error: str | None = None
    source_ids: str
    created_at: datetime
    model_config = {"from_attributes": True}


class AdmakeProjectOut(BaseModel):
    id: int
    title: str
    description: str | None
    prompt_template: str
    target_platform: str
    ai_model: str = "gemini-3.5-flash"
    editor_alias: str = ""
    default_template_id: int | None = None
    export_path: str | None = None
    drama_project_id: int | None
    created_at: datetime
    updated_at: datetime
    source_count: int = 0
    part_count: int = 0
    # 全部任务完成 = 有 Part 且每个 Part 都合成完(ready)、AI 分析完(done)；列表页封面深浅色用
    all_done: bool = False
    model_config = {"from_attributes": True}


class AdmakeProjectDetailOut(AdmakeProjectOut):
    sources: list[AdmakeSourceOut] = []
    parts: list[AdmakePartOut] = []


# ── Marks ─────────────────────────────────────────────────────

class AdmakeMarkIn(BaseModel):
    mark_type: str  # highlight_start | highlight_end | hook_start | hook_end
    group_index: int = 0
    time_sec: float
    score: int | None = None


class AdmakeMarkBatchSave(BaseModel):
    marks: list[AdmakeMarkIn]


class AdmakeMarkOut(BaseModel):
    id: int
    mark_type: str
    group_index: int
    time_sec: float
    score: int | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


# ── 段库 ──────────────────────────────────────────────────────

class AdmakePlotPointOut(BaseModel):
    id: int
    time_sec: float
    start_sec: float | None = None
    end_sec: float | None = None
    kind: str | None = None
    text: str = ""
    story_score: float = 0.0
    intensity_score: int = 0
    model_config = {"from_attributes": True}


class AdmakeSegmentOut(BaseModel):
    id: int
    part_id: int
    seg_index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    excluded: bool = False
    seg_type: str | None = None
    intensity: int = 0
    # 其余三十来个字段原样带出去，键名跟拆解产物一致，前端按名取
    fields: dict = {}
    plot_points: list[AdmakePlotPointOut] = []


class AdmakeSegmentPatch(BaseModel):
    """右栏能改的只有一件事：这一段用不用。

    改段的起止属于精修，那件事去剪映做，工具只管把段拆出来给剪辑核对。
    """
    excluded: bool


# ── Exports ───────────────────────────────────────────────────

class AdmakeExportRequest(BaseModel):
    mode: str                        # highlight_shunjian | xuanyi_daozhi | shuanggaoguang
    count: int = 1                   # 要生成的素材数量
    target_duration: float = 360.0   # 目标成片时长（秒）。选材按离这个值最近排，不是硬约束
    part_ids: list[int] | None = None  # 视频轨上的 Part ID（有序），None 表示使用项目所有 Part
    template_id: int | None = None   # 成片模板（前端预填项目默认模板，可当场改）；None=不套模板


class AdmakeTaskOut(BaseModel):
    """任务中心行：一个项目里一类进行中的任务。完成即从列表消失。"""
    type: str          # export / ai / parts
    project_id: int
    title: str
    count: int


class AdmakeExportOut(BaseModel):
    id: int
    part_id: int
    project_id: int | None = None
    mode: str
    status: str
    file_path: str | None
    duration_sec: float
    # 非空 = 成片正常但模板没套上（原因文案），历史里要提示
    template_error: str | None = None
    created_at: datetime
    model_config = {"from_attributes": True}

    @computed_field
    @property
    def log_path(self) -> str | None:
        if self.file_path and self.file_path.endswith(".mp4"):
            return self.file_path[:-4] + ".log.txt"
        return None


# ── 成片模板 ───────────────────────────────────────────────────

class AdmakeTemplateCreate(BaseModel):
    name: str
    config: dict = {}


class AdmakeTemplateUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None


class AdmakeTemplateOut(BaseModel):
    id: int
    name: str
    config: dict            # DB 里存 JSON 字符串，路由层解析成 dict 再返回
    thumb_path: str | None = None
    is_default: bool = False
    created_at: datetime
    updated_at: datetime

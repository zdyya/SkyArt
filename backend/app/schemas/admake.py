from pydantic import BaseModel, computed_field
from datetime import datetime


class AdmakeProjectCreate(BaseModel):
    title: str
    description: str | None = None
    prompt_template: str = "默认模板"
    target_platform: str = "TikTok"
    ai_model: str = "gemini-3.5-flash"
    # marks=按高光钩子剪，segments=按模板剪。建完不可改。
    project_kind: str = "marks"
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
    # 这个 Part 拆出了几个段。按模板剪的项目没有标记，左侧列表的统计和
    # 「哪些 Part 分析过」都要靠它，不能再去数标记。
    seg_count: int = 0
    # 真的在跑却半小时没动静。项目卡片上会显示「可能卡住 N」，点进来这一条要
    # 能对上号，并且给得出一个「重建」按钮——不然用户只看到一个转不停的圈。
    stuck: bool = False
    created_at: datetime
    model_config = {"from_attributes": True}


class AdmakeProjectOut(BaseModel):
    id: int
    title: str
    description: str | None
    prompt_template: str
    target_platform: str
    ai_model: str = "gemini-3.5-flash"
    project_kind: str = "marks"
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
    # 项目卡片上的红字。合成失败几个、分析失败几个、疑似卡住几个，
    # 不用点进项目逐个 Part 数。
    failed_parts: int = 0
    failed_ai: int = 0
    stuck_parts: int = 0
    # 片头片尾检测挂掉的源有几条。这几集的片尾定格板不会被切掉，成片末尾会多一截。
    trim_failed: int = 0
    # 卡片上那两条进度的分子：合成好的 Part 几个、分析完的 Part 几个。
    # 分母是 part_count。Part 还没建出来时 part_count 为 0，卡片改显示「正在准备 Part…」。
    ready_parts: int = 0
    analyzed_parts: int = 0
    # 导出过没有。卡片上光看合成和分析两条进度，分不出哪个项目已经出过片：
    # 十几个项目排在一起，隔一天回来就得逐个点进去看导出历史。
    exported_count: int = 0      # 已经出好的成片条数（status=ready）
    exporting_count: int = 0     # 正在导出的条数（pending / processing）
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
    # 剪辑模板：按模板剪专用，声明每一格要什么样的段。跟上面那个成片模板是两回事。
    # 一次可以选几个，count 是这几个模板一共出几条，平均分下去。
    cut_template_ids: list[int] | None = None
    # 旧字段，只收一个。留着是为了老版本界面发来的请求还能用。
    cut_template_id: int | None = None


class AdmakeCutTemplateIn(BaseModel):
    name: str
    source: str = "剪辑自定义模板"
    source_note: str = ""
    kind: str = "整条"
    slots: list[dict] = []          # 「格」数组，每格声明条件，形状见 取段.py


class AdmakeCutTemplateOut(BaseModel):
    id: int
    name: str
    source: str
    source_note: str
    kind: str
    slots: list[dict] = []
    builtin: bool = False
    created_at: datetime
    model_config = {"from_attributes": True}


class AdmakeTaskOut(BaseModel):
    """任务中心行：一个项目里一类进行中的任务。完成即从列表消失。"""
    type: str          # export / ai / parts
    project_id: int
    title: str
    count: int
    project_kind: str = "marks"


class AdmakeHomeSummaryOut(BaseModel):
    """首页左下角那块数字。打开首页取一次，不轮询。"""
    running: int                 # 在跑的任务数（一个项目里一类任务算一个）
    parts_done_today: int        # 今天分析完的 Part 数
    exports_today: int           # 今天导出成功的成片条数
    running_items: list[AdmakeTaskOut] = []
    # 第一条在跑任务的进度，只有 AI 分析有：progress/total
    progress: int | None = None
    total: int | None = None
    # 失败的任务。原来只有在项目里逐个 Part 看才知道哪条挂了，一个项目十几个 Part
    # 失败时要翻一遍才数得清，首页直接把它摆出来，并给一个重试入口。
    failed: int = 0
    failed_items: list[AdmakeTaskOut] = []


class AdmakeExportOut(BaseModel):
    fail_reason: str | None = None
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
    # 这条片子是哪个剪辑模板剪的。导出历史要按模板分组显示
    cut_template_id: int | None = None
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

from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Text, Float, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class AdmakeProject(Base):
    __tablename__ = "admake_projects"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template: Mapped[str] = mapped_column(String(64), default="默认模板")
    target_platform: Mapped[str] = mapped_column(String(64), default="TikTok")
    ai_model: Mapped[str] = mapped_column(String(64), default="gemini-3.5-flash")
    # 项目类型：marks=按高光钩子剪（老功能），segments=按模板剪（段库）。
    # 建项目时定死，之后不改——两种类型的分析产物和取数逻辑不通用。
    project_kind: Mapped[str] = mapped_column(String(32), default="marks")
    editor_alias: Mapped[str] = mapped_column(String(20), default="")
    export_seq: Mapped[int] = mapped_column(Integer, default=0)
    # 自定义导出目录（本地绝对路径）。空=用 App 数据目录内的默认导出目录。
    export_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 项目默认成片模板：导出面板预填这个模板，可当场改；模板被删时置空（SET NULL）。
    default_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("admake_templates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    drama_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    sources: Mapped[list["AdmakeSource"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="AdmakeSource.sort_order"
    )
    parts: Mapped[list["AdmakePart"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="AdmakePart.part_index"
    )


class AdmakeSource(Base):
    __tablename__ = "admake_sources"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("admake_projects.id", ondelete="CASCADE"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 片头/片尾转场段检测结果（定格/静图缓拉，秒）。0=没有或未检测。当前只标注，建 Part 暂不裁剪
    head_trim: Mapped[float] = mapped_column(Float, default=0.0)
    tail_trim: Mapped[float] = mapped_column(Float, default=0.0)
    # 片头片尾检测跑过没有：none 没跑 / ok 跑成了 / failed 跑挂了。
    # 原来只有 head_trim、tail_trim 两个数，0 有两个意思——「跑过，这集本来就没有
    # 片尾板」和「跑挂了」分不开，于是挂掉的那条永远不会重试，界面也提示不了，
    # 那集的片尾定格板从此再也切不掉。
    trim_state: Mapped[str] = mapped_column(String(16), default="none")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    project: Mapped["AdmakeProject"] = relationship(back_populates="sources")


class AdmakePart(Base):
    """合并后的分 Part 视频（drama_Part1, drama_Part2, …）。"""
    __tablename__ = "admake_parts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("admake_projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    part_index: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/processing/ready/failed
    ai_status: Mapped[str] = mapped_column(String(32), default="none")  # none/analyzing/done/failed
    ai_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # AI 分析是什么时候跑完的。Part 表原来只有状态没有时间，首页要数「今天分析完
    # 几个」只能去数标记的创建时间，而人工拖一个标记点按保存就会把那个时间刷新，
    # 今天没分析过也显示分析完了 1 个。
    ai_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of AdmakeSource IDs
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # 这一行最后变动的时间。用来判断「卡住」：合成中的 Part 半小时没有任何动静，
    # 多半是执行它的进程已经不在了。原来只有 created_at，重建过的 Part 时间还是
    # 当初分 Part 那一刻，拿它算卡了多久会把所有老 Part 都算成卡住。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    project: Mapped["AdmakeProject"] = relationship(back_populates="parts")
    marks: Mapped[list["AdmakeMark"]] = relationship(
        back_populates="part", cascade="all, delete-orphan", order_by="AdmakeMark.group_index, AdmakeMark.time_sec"
    )
    exports: Mapped[list["AdmakeExport"]] = relationship(
        back_populates="part", cascade="all, delete-orphan", order_by="AdmakeExport.created_at.desc()"
    )
    segments: Mapped[list["AdmakeSegment"]] = relationship(
        back_populates="part", cascade="all, delete-orphan",
        order_by="AdmakeSegment.seg_index",
    )


class AdmakeMark(Base):
    """标记点：高光开始/结束、钩子开始/结束。"""
    __tablename__ = "admake_marks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("admake_parts.id", ondelete="CASCADE"), index=True
    )
    # highlight_start | highlight_end | hook_start | hook_end
    mark_type: Mapped[str] = mapped_column(String(32), nullable=False)
    group_index: Mapped[int] = mapped_column(Integer, default=0)
    time_sec: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    part: Mapped["AdmakePart"] = relationship(back_populates="marks")


class AdmakeCutTemplate(Base):
    """剪辑模板：一张「格」的清单，每格声明这一格要什么样的段。

    跟 AdmakeTemplate（成片模板，管前后贴片、花字、贴纸）是两回事，不要混。
    导出请求里成片模板叫 template_id，这个叫 cut_template_id。
    """
    __tablename__ = "admake_cut_templates"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 爆款模板＝从跑量素材里拆出来的，剪辑自定义模板＝人写的
    source: Mapped[str] = mapped_column(String(32), default="爆款模板")
    source_note: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(16), default="整条")   # 整条 / 片段
    # 格数组的 JSON。照 AdmakeTemplate 的约定：库里存字符串，路由层解析成 dict 再返回
    config: Mapped[str] = mapped_column(Text, default="[]")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)   # 随包预置的，不给删
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AdmakeSegment(Base):
    """AI 从一个 Part 里拆出来的一段戏。

    列只留三种：外键和时间（要按它查和排序）、界面和模板引擎每次都要读的两个
    （段落类型上色、尺度档过滤）、剪辑排除的开关。其余三十来个字段整块存进
    fields_json，键名跟拆解产物一字不差。

    这么分是因为字段表还在动，逐列建表每改一个字段就要补一条 ALTER；而套模板
    选段本来就是把整个项目的段全读进内存算，不靠 SQL 过滤。
    """
    __tablename__ = "admake_segments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("admake_parts.id", ondelete="CASCADE"), index=True
    )
    seg_index: Mapped[int] = mapped_column(Integer, default=0)
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    # 剪辑在右栏把这一段排除掉，导出时不进任何一条成片
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # 冲突 / 反转 / 爽点 / 悬念点 / 情绪，判不出来是 None
    seg_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 尺度档 0 到 4，没命中行为词是 0
    intensity: Mapped[int] = mapped_column(Integer, default=0)
    fields_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    part: Mapped["AdmakePart"] = relationship(back_populates="segments")
    plot_points: Mapped[list["AdmakePlotPoint"]] = relationship(
        back_populates="segment", cascade="all, delete-orphan",
        order_by="AdmakePlotPoint.time_sec",
    )


class AdmakePlotPoint(Base):
    """段里的一个剧情点。

    单独一张表而不是塞进段的 JSON：界面按时间列它们、点一条跳过去，
    套模板时又要按剧情分挑代表，两处都要逐条读。
    """
    __tablename__ = "admake_plot_points"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("admake_segments.id", ondelete="CASCADE"), index=True
    )
    time_sec: Mapped[float] = mapped_column(Float, nullable=False)
    # 配给这个点的起止，用在进度条刻度和超长段进格时裁哪一截
    start_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    story_score: Mapped[float] = mapped_column(Float, default=0.0)
    intensity_score: Mapped[int] = mapped_column(Integer, default=0)
    fields_json: Mapped[str] = mapped_column(Text, default="{}")

    segment: Mapped["AdmakeSegment"] = relationship(back_populates="plot_points")


class AdmakeExport(Base):
    """素材导出任务。"""
    __tablename__ = "admake_exports"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("admake_parts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("admake_projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    # 这批导出提交时填的目标时长（秒）。用来统计每个人常用哪几档，导出面板拿它做快捷值。
    target_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 按模板剪用的：这一批套的哪个剪辑模板、每一格填了哪个段
    cut_template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 模板套用失败原因（成片本体成功、只有模板没套上时非空）——历史里要标出来，
    # 否则批量导出全是"没套模板的白片"也显示就绪，运营不知情
    template_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 这一条为什么失败。只有 status=failed 时才有值。以前失败原因只写进后端日志，
    # 界面上就一个「失败」，用户没法知道是片源丢了还是磁盘满了
    fail_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    part: Mapped["AdmakePart"] = relationship(back_populates="exports")


class AdmakeTemplate(Base):
    """成片模板：一部剧的包装配置（前后贴片 + 文字 + 贴纸 + 特效 + 闪白），
    全局保存、跨项目复用。config 存结构 + 变量占位 + 样式，导出时取项目字段替换变量再渲染。
    素材文件另存于 {UPLOAD_DIR}/AdMake/Templates/{id}/，config 里按文件名引用。"""
    __tablename__ = "admake_templates"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    config: Mapped[str] = mapped_column(Text, default="{}")  # JSON：intro/outro/texts/pngs/dyns/transition
    thumb_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 默认模板：新建项目和导出面板在没选模板时预填它。每个用户各自一个，
    # 设新的会把这个人其它模板的标记清掉（见 set_default_template）。
    # "不套模板"仍是可选项——预填不等于强制，用户在下拉里改成"不使用"照样生效。
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

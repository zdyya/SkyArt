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
    source_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of AdmakeSource IDs
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    project: Mapped["AdmakeProject"] = relationship(back_populates="parts")
    marks: Mapped[list["AdmakeMark"]] = relationship(
        back_populates="part", cascade="all, delete-orphan", order_by="AdmakeMark.group_index, AdmakeMark.time_sec"
    )
    exports: Mapped[list["AdmakeExport"]] = relationship(
        back_populates="part", cascade="all, delete-orphan", order_by="AdmakeExport.created_at.desc()"
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
    # 模板套用失败原因（成片本体成功、只有模板没套上时非空）——历史里要标出来，
    # 否则批量导出全是"没套模板的白片"也显示就绪，运营不知情
    template_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
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

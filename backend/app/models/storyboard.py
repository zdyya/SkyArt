from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class StoryboardScene(Base):
    __tablename__ = "storyboard_scenes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    clip_id: Mapped[int | None] = mapped_column(
        ForeignKey("timeline_clips.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scene_label: Mapped[str] = mapped_column(String(32), nullable=False)  # SC-01, SC-02…
    order: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="草稿")

    # Shot metadata
    shot_type: Mapped[str] = mapped_column(String(32), default="全景")
    angle: Mapped[str] = mapped_column(String(32), default="平视")
    weather: Mapped[str] = mapped_column(String(32), default="晴天")
    camera_move: Mapped[str] = mapped_column(String(32), default="固定")
    time_period: Mapped[str] = mapped_column(String(32), default="白天")
    script: Mapped[str] = mapped_column(Text, default="")

    # FFmpeg 裁切的原始片段（每次导出时更新，generate_video 用此作为源视频）
    clip_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 上次导出时记录的裁切点快照，用于增量导出比较
    exported_start: Mapped[float | None] = mapped_column(nullable=True)
    exported_end:   Mapped[float | None] = mapped_column(nullable=True)
    # 当前选定的视频（AI生成或原始裁切）
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 所有AI生成视频路径列表（JSON数组），累积保留不覆盖
    generated_videos: Mapped[str] = mapped_column(Text, default="[]")
    # Async generation task id (HappyHorse task)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    episode: Mapped["Episode"] = relationship(back_populates="storyboard_scenes")  # noqa: F821
    refs: Mapped[list["StoryboardRef"]] = relationship(
        back_populates="scene", cascade="all, delete-orphan"
    )


class StoryboardRef(Base):
    """Links a storyboard scene to a character/scene/prop reference."""
    __tablename__ = "storyboard_refs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    scene_id: Mapped[int] = mapped_column(
        ForeignKey("storyboard_scenes.id", ondelete="CASCADE"), index=True
    )
    ref_type: Mapped[str] = mapped_column(String(16), nullable=False)  # char | scene | prop
    ref_asset_id: Mapped[int] = mapped_column(nullable=False)  # FK to char/scene/prop table
    ref_name: Mapped[str] = mapped_column(String(100), nullable=False)  # denormalized for quick display

    scene: Mapped["StoryboardScene"] = relationship(back_populates="refs")

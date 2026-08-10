from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Float, Integer, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="草稿")
    duration: Mapped[str] = mapped_column(String(32), default="")
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    total_duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship(back_populates="episodes")  # noqa: F821
    clips: Mapped[list["TimelineClip"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan", order_by="TimelineClip.order"
    )
    storyboard_scenes: Mapped[list["StoryboardScene"]] = relationship(  # noqa: F821
        back_populates="episode", cascade="all, delete-orphan", order_by="StoryboardScene.order"
    )


class TimelineClip(Base):
    __tablename__ = "timeline_clips"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    start_pct: Mapped[float] = mapped_column(Float, nullable=False)
    end_pct: Mapped[float] = mapped_column(Float, nullable=False)
    color: Mapped[int] = mapped_column(Integer, default=1)
    order: Mapped[int] = mapped_column(Integer, default=0)
    # Actual video clip file (after FFmpeg export)
    clip_video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    episode: Mapped["Episode"] = relationship(back_populates="clips")

    @property
    def start_sec(self) -> float:
        return self.episode.total_duration_sec * self.start_pct / 100

    @property
    def end_sec(self) -> float:
        return self.episode.total_duration_sec * self.end_pct / 100

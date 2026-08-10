from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_type: Mapped[str] = mapped_column(String(32), default="短剧")
    platform: Mapped[str] = mapped_column(String(64), default="TikTok")
    resolution: Mapped[str] = mapped_column(String(16), default="1080P")
    drama_style: Mapped[str] = mapped_column(String(32), default="古装")
    art_style: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt_template: Mapped[str] = mapped_column(String(64), default="默认模板")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner: Mapped["User"] = relationship(back_populates="projects")  # noqa: F821
    episodes: Mapped[list["Episode"]] = relationship(back_populates="project", cascade="all, delete-orphan")  # noqa: F821
    characters: Mapped[list["Character"]] = relationship(back_populates="project", cascade="all, delete-orphan")  # noqa: F821
    scene_settings: Mapped[list["SceneSetting"]] = relationship(back_populates="project", cascade="all, delete-orphan")  # noqa: F821
    props: Mapped[list["Prop"]] = relationship(back_populates="project", cascade="all, delete-orphan")  # noqa: F821

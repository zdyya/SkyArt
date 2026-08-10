"""Characters, SceneSettings, Props — the three reference asset types."""
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


# ── Character ───────────────────────────────────────────────────────────────

class Character(Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    gender: Mapped[str] = mapped_column(String(16), default="男")
    age: Mapped[str] = mapped_column(String(16), default="")
    appearance: Mapped[str | None] = mapped_column(Text, nullable=True)
    personality: Mapped[str | None] = mapped_column(Text, nullable=True)
    ethnicity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="草稿")
    base_image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    project: Mapped["Project"] = relationship(back_populates="characters")  # noqa: F821
    images: Mapped[list["AssetImage"]] = relationship(
        "AssetImage",
        primaryjoin="and_(AssetImage.asset_type=='character', AssetImage.asset_id==Character.id)",
        foreign_keys="[AssetImage.asset_id]",
        cascade="all, delete-orphan",
        overlaps="scene_images,prop_images",
    )


# ── SceneSetting ─────────────────────────────────────────────────────────────

class SceneSetting(Base):
    __tablename__ = "scene_settings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_of_day: Mapped[str] = mapped_column(String(32), default="白天")
    weather: Mapped[str] = mapped_column(String(32), default="晴天")
    status: Mapped[str] = mapped_column(String(32), default="草稿")
    base_image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    project: Mapped["Project"] = relationship(back_populates="scene_settings")  # noqa: F821


# ── Prop ─────────────────────────────────────────────────────────────────────

class Prop(Base):
    __tablename__ = "props"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    aliases: Mapped[str] = mapped_column(Text, default="[]")  # JSON array string
    status: Mapped[str] = mapped_column(String(32), default="草稿")
    base_image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    project: Mapped["Project"] = relationship(back_populates="props")  # noqa: F821


# ── AssetImage (candidate images for char/scene/prop) ────────────────────────

class AssetImage(Base):
    __tablename__ = "asset_images"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)  # character|scene|prop
    asset_id: Mapped[int] = mapped_column(nullable=False, index=True)
    image_path: Mapped[str] = mapped_column(String(500), nullable=False)
    is_base: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

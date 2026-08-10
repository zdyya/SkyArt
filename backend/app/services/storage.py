"""File storage helpers — saves uploaded files and returns relative URL paths."""
import os
import re
import uuid
import aiofiles
from fastapi import UploadFile
from app.core.config import settings

ALLOWED_VIDEO = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_VIDEO_SIZE = 4 * 1024 * 1024 * 1024  # 4 GB
MAX_IMAGE_SIZE = 20 * 1024 * 1024         # 20 MB


def _ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


# ── Project directory helpers ────────────────────────────────────────────────

def slugify(name: str) -> str:
    """Convert any string to a filesystem-safe slug.
    Keeps Chinese characters and alphanumerics; replaces everything else with '-'.
    """
    slug = re.sub(r'[^\w一-鿿]+', '-', name)
    slug = slug.strip('-')[:40]
    return slug or 'unnamed'


def get_project_dir(project_id: int, project_name: str) -> str:
    """Returns '{project_id}_{slug}' — stable (ID) + human-readable (name)."""
    return f"{project_id}_{slugify(project_name)}"


def ensure_dir(rel_path: str) -> str:
    """Create directories under UPLOAD_DIR if they don't exist; return rel_path."""
    abs_p = os.path.join(settings.UPLOAD_DIR, rel_path)
    os.makedirs(abs_p, exist_ok=True)
    return rel_path


# ── Upload helpers ────────────────────────────────────────────────────────────

async def save_video(file: UploadFile, project_dir: str | None = None) -> str:
    """Save uploaded video under videos/{project_dir}/{uuid}.ext
    Returns relative path.
    """
    ext = _ext(file.filename or ".mp4")
    if ext not in ALLOWED_VIDEO:
        raise ValueError(f"不支持的视频格式: {ext}")
    filename = f"{uuid.uuid4().hex}{ext}"
    subdir = os.path.join("DramaCreate", "videos", project_dir) if project_dir else os.path.join("DramaCreate", "videos")
    ensure_dir(subdir)
    rel_path = os.path.join(subdir, filename)
    abs_p = os.path.join(settings.UPLOAD_DIR, rel_path)
    async with aiofiles.open(abs_p, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)
    return rel_path


async def save_image(file: UploadFile, subfolder: str = "DramaCreate/images") -> str:
    """Save uploaded image; return relative path like 'images/uuid.jpg'."""
    ext = _ext(file.filename or ".jpg")
    if ext not in ALLOWED_IMAGE:
        raise ValueError(f"不支持的图片格式: {ext}")
    filename = f"{uuid.uuid4().hex}{ext}"
    ensure_dir(subfolder)
    rel_path = os.path.join(subfolder, filename)
    abs_p = os.path.join(settings.UPLOAD_DIR, rel_path)
    async with aiofiles.open(abs_p, "wb") as f:
        content = await file.read()
        if len(content) > MAX_IMAGE_SIZE:
            raise ValueError("图片大小超过 20MB 限制")
        await f.write(content)
    return rel_path


def abs_path(rel: str) -> str:
    """Convert relative upload path to absolute filesystem path."""
    return os.path.abspath(os.path.join(settings.UPLOAD_DIR, rel))


def url_path(rel: str) -> str:
    """Convert relative path to API URL path."""
    return f"/uploads/{rel}"

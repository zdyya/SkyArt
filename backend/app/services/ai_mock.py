"""
Mock AI service — simulates image and video generation.
Replace the _call_real_* methods with actual API calls once keys are available.

Slot structure:
  - generate_image(asset_type, asset_id, prompt, count) → list[image_paths]
  - generate_storyboard_video(scene_id, refs, prompt) → video_path
"""
import asyncio
import os
import uuid
from app.core.config import settings


# Gradient placeholders for mock images (SVG base64-free approach — write solid-color PNGs)
MOCK_COLORS = [
    (99, 102, 241),   # indigo
    (16, 185, 129),   # emerald
    (245, 158, 11),   # amber
    (239, 68, 68),    # red
    (59, 130, 246),   # blue
    (236, 72, 153),   # pink
]


def _create_placeholder_image(color: tuple[int, int, int], width: int = 512, height: int = 512) -> bytes:
    """Create a minimal solid-color PNG in pure Python (no Pillow dependency at runtime)."""
    import struct, zlib

    r, g, b = color

    def chunk(name: bytes, data: bytes) -> bytes:
        c = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", c)

    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes([r, g, b] * width)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend


async def generate_image(
    asset_type: str,
    asset_id: int,
    prompt: str = "",
    count: int = 4,
    model_api: str = "QWen",
) -> list[str]:
    """
    Generate (mock) images for a character/scene/prop.
    Returns list of relative paths under uploads/images/.

    TODO: Replace with real API call:
        if settings.QWEN_API_KEY:
            return await _call_qwen_image(prompt, count)
    """
    # Simulate latency
    await asyncio.sleep(0.3)

    paths = []
    for i in range(count):
        color = MOCK_COLORS[(asset_id + i) % len(MOCK_COLORS)]
        filename = f"{uuid.uuid4().hex}.png"
        abs_p = os.path.join(settings.UPLOAD_DIR, "DramaCreate", "images", filename)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "wb") as f:
            f.write(_create_placeholder_image(color))
        paths.append(os.path.join("DramaCreate", "images", filename))

    return paths


async def generate_storyboard_video(
    scene_id: int,
    refs: list[dict],
    script: str = "",
    model_api: str = "HappyHorse",
) -> str:
    """
    Generate (mock) video for a storyboard scene.
    Returns relative path under uploads/exports/.

    TODO: Replace with real API call:
        if settings.HAPPYHORSE_API_KEY:
            return await _call_happyhorse_video(refs, script)
    """
    await asyncio.sleep(0.5)

    # For mock, just return a placeholder path (no actual video file created)
    filename = f"mock_{scene_id}_{uuid.uuid4().hex[:8]}.mp4"
    rel_path = os.path.join("exports", filename)
    # Create an empty placeholder file so the path is "valid"
    abs_p = os.path.join(settings.UPLOAD_DIR, rel_path)
    with open(abs_p, "wb") as f:
        f.write(b"")  # empty placeholder
    return rel_path


# ── Real API stubs (uncomment and implement when keys are ready) ──────────────

# async def _call_qwen_image(prompt: str, count: int) -> list[str]:
#     import httpx
#     async with httpx.AsyncClient() as client:
#         resp = await client.post(
#             f"{settings.QWEN_BASE_URL}/services/aigc/text2image/image-synthesis",
#             headers={"Authorization": f"Bearer {settings.QWEN_API_KEY}"},
#             json={"model": "wanx-v1", "input": {"prompt": prompt}, "parameters": {"n": count}},
#         )
#         resp.raise_for_status()
#         # Download and save images…
#         ...

# async def _call_happyhorse_video(refs: list[dict], script: str) -> str:
#     import httpx
#     async with httpx.AsyncClient(timeout=300) as client:
#         resp = await client.post(
#             f"{settings.HAPPYHORSE_BASE_URL}/generate",
#             headers={"Authorization": f"Bearer {settings.HAPPYHORSE_API_KEY}"},
#             json={"references": refs, "script": script},
#         )
#         resp.raise_for_status()
#         ...

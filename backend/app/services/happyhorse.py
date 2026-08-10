"""
HappyHorse (DashScope) video-edit service.

文件上传流程
────────────
DashScope 视频编辑接口需要公网可访问的 URL。
本地文件先上传到阿里云 OSS，再把 OSS 公网 URL 传给接口。

配置（.env）：
  OSS_ACCESS_KEY_ID      / OSS_ACCESS_KEY_SECRET
  OSS_BUCKET_NAME        — bucket 名称（需开启公共读，或改用签名 URL）
  OSS_ENDPOINT           — 如 https://oss-cn-hangzhou.aliyuncs.com
  OSS_CUSTOM_DOMAIN      — 可选，绑定的自定义域名（不填则用默认 bucket 域名）
  HAPPYHORSE_BASE_URL    — https://dashscope.aliyuncs.com/api/v1（国内节点）

无 API key 时自动 fallback 到 mock 模式。
"""
import asyncio
import os
import time
import httpx
import oss2
from app.core.config import settings
from app.services.storage import abs_path

# ── OSS 上传 ──────────────────────────────────────────────────────────────────

_MIME_MAP = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}

_OSS_PREFIX = "skyart-tmp/"   # OSS 目录前缀，方便统一管理/清理


def _get_oss_bucket() -> oss2.Bucket:
    """返回已认证的 OSS Bucket 对象。"""
    if not (settings.OSS_ACCESS_KEY_ID and settings.OSS_ACCESS_KEY_SECRET and settings.OSS_BUCKET_NAME):
        raise RuntimeError(
            "OSS 未配置，请在 .env 中填写 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / "
            "OSS_BUCKET_NAME / OSS_ENDPOINT"
        )
    auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
    return oss2.Bucket(auth, settings.OSS_ENDPOINT, settings.OSS_BUCKET_NAME)


async def _upload_to_oss(abs_file_path: str) -> str:
    """
    把本地文件上传到 OSS，返回带签名的临时公网 URL（有效期 1 小时）。
    使用预签名 URL 避免要求 bucket 开公共读。
    使用线程池执行 oss2（同步 SDK），避免阻塞 event loop。
    """
    if not os.path.exists(abs_file_path):
        raise FileNotFoundError(f"本地文件不存在: {abs_file_path}")

    filename = os.path.basename(abs_file_path)
    ext = os.path.splitext(filename)[1].lower()
    mime = _MIME_MAP.get(ext, "application/octet-stream")

    # 用时间戳+随机避免文件名冲突
    oss_key = f"{_OSS_PREFIX}{int(time.time())}_{os.urandom(4).hex()}_{filename}"

    def _do_upload_and_sign():
        bucket = _get_oss_bucket()
        with open(abs_file_path, "rb") as f:
            bucket.put_object(
                oss_key, f,
                headers={"Content-Type": mime},
            )
        # 生成预签名 URL，有效期 3600 秒（1 小时）
        # 使用 slash_safe=True 防止 key 中的 / 被二次编码
        signed_url = bucket.sign_url("GET", oss_key, 3600, slash_safe=True)
        return signed_url

    loop = asyncio.get_running_loop()
    signed_url = await loop.run_in_executor(None, _do_upload_and_sign)
    return signed_url


# ── Public API ────────────────────────────────────────────────────────────────

async def submit_video_edit(
    prompt: str,
    video_rel_path: str,
    ref_image_rel_paths: list[str],
    resolution: str = "1080P",
) -> str:
    """
    提交异步视频编辑任务，返回 task_id。
    未配置 API key 时返回 mock task_id。
    """
    if not settings.HAPPYHORSE_API_KEY:
        return f"mock-task-{os.urandom(4).hex()}"

    # ── 1. 解析绝对路径 ──────────────────────────────────────────────────────
    video_abs = abs_path(video_rel_path) if not os.path.isabs(video_rel_path) else video_rel_path
    img_abs_list = [
        (abs_path(r) if not os.path.isabs(r) else r)
        for r in ref_image_rel_paths
    ]

    # ── 2. 上传到 OSS，获取公网 URL ──────────────────────────────────────────
    video_url = await _upload_to_oss(video_abs)

    media = [{"type": "video", "url": video_url}]
    for img_abs in img_abs_list:
        img_url = await _upload_to_oss(img_abs)
        media.append({"type": "reference_image", "url": img_url})

    # ── 3. 调用 HappyHorse 视频编辑接口 ─────────────────────────────────────
    payload = {
        "model": "happyhorse-1.0-video-edit",
        "input": {
            "prompt": prompt,
            "media": media,
        },
        "parameters": {
            "resolution": resolution,
        },
    }

    api_url = f"{settings.HAPPYHORSE_BASE_URL}/services/aigc/video-generation/video-synthesis"
    headers = {
        "Authorization": f"Bearer {settings.HAPPYHORSE_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(api_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    import json as _json
    print(f"[HappyHorse] submit response:\n{_json.dumps(data, ensure_ascii=False, indent=2)}")

    # DashScope 异步响应：{"output": {"task_id": "...", "task_status": "PENDING"}, ...}
    task_id = data.get("output", {}).get("task_id") or data.get("request_id", "")
    if not task_id:
        raise ValueError(f"接口未返回 task_id: {data}")
    print(f"[HappyHorse] submitted task_id={task_id}")
    return task_id


async def get_task_status(task_id: str) -> dict:
    """
    轮询任务状态。
    返回 {status, output_url, error_message}
    status 取值：PENDING | RUNNING | SUCCEEDED | FAILED
    """
    if task_id.startswith("mock-task-"):
        return {"status": "SUCCEEDED", "output_url": None, "error_message": None}

    if not settings.HAPPYHORSE_API_KEY:
        return {"status": "FAILED", "output_url": None, "error_message": "No API key"}

    task_url = f"{settings.HAPPYHORSE_BASE_URL}/tasks/{task_id}"
    headers = {"Authorization": f"Bearer {settings.HAPPYHORSE_API_KEY}"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(task_url, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    # ── 打印完整响应，便于调试 ───────────────────────────────────────────────
    import json as _json
    print(f"[HappyHorse] task_status raw response:\n{_json.dumps(data, ensure_ascii=False, indent=2)}")

    output = data.get("output", {})
    status = output.get("task_status", "UNKNOWN")

    # 兼容多种字段名：video_url / url / results[].url / videos[].url
    output_url = None
    if status == "SUCCEEDED":
        output_url = (
            output.get("video_url")
            or output.get("url")
            or (output.get("results") or [{}])[0].get("url")
            or (output.get("videos") or [{}])[0].get("url")
        )

    print(f"[HappyHorse] parsed status={status}, output_url={output_url}")

    return {
        "status": status,
        "output_url": output_url,
        "error_message": output.get("message") or output.get("error") if status == "FAILED" else None,
        "_raw_output": output,   # 前端调试用，可看到完整 output 字段
    }

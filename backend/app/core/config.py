from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List
import json
import os


class Settings(BaseSettings):
    # Security
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 43200  # 30 days

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./skyart.db"

    # File storage
    # 一律存成绝对路径。装好的 app 由 Electron 传绝对路径进来，从没暴露过问题；
    # 而相对路径会被拼两次——admake 先 join 出 ./uploads/AdMake/…，video.get_video_info
    # 看它不是绝对路径又 join 一次，变成 uploads/uploads/AdMake/…，ffprobe 找不到文件，
    # 报出来的却是「素材时长探测失败，文件可能已损坏」。
    UPLOAD_DIR: str = "./uploads"

    # AI API (填入真实 Key 后生效)
    # 阿里云 MaaS 白名单专属通道。
    # 区域锁死在美国 us-east-1：这条通道的 key 打公网 dashscope 或其他区域地址
    # 一律报 invalid_api_key，Host 和 key 必须成对使用。
    # 通道内每个请求都要带 X-DashScope-DataInspection 关绿网，不带报 access_denied。
    HAPPYHORSE_API_KEY: str = ""
    HAPPYHORSE_BASE_URL: str = "https://ws-bljb9l73522yj7vp.us-east-1.maas.aliyuncs.com/api/v1"
    QWEN_API_KEY: str = ""
    QWEN_BASE_URL: str = "https://ws-bljb9l73522yj7vp.us-east-1.maas.aliyuncs.com/api/v1"

    # Gemini 代理（Dscript 海外服务，替代直连 Gemini）
    GEMINI_PROXY_URL: str = ""   # 如 https://api.dscript.com/skyart/gemini/analyze
    GEMINI_PROXY_KEY: str = ""   # 双方协商的共享 API Key

    # 阿里云 OSS（国内区）— HappyHorse 等
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_BUCKET_NAME: str = ""
    OSS_ENDPOINT: str = "https://oss-cn-hangzhou.aliyuncs.com"
    OSS_CUSTOM_DOMAIN: str = ""

    # 阿里云 OSS（美区）— Gemini 代理上传用
    OSS_KEY_SkyArt_US: str = ""
    OSS_SECRET_SkyArt_US: str = ""
    OSS_BUCKET_SkyArt_US: str = ""
    OSS_ENDPOINT_SkyArt_US: str = "https://oss-us-west-1.aliyuncs.com"

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "app://.",
        "file://",
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return [i.strip() for i in v.split(",")]
        return v

    @field_validator("UPLOAD_DIR")
    @classmethod
    def _素材目录转绝对(cls, v: str) -> str:
        return os.path.abspath(os.path.expanduser(v or "./uploads"))

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

# Ensure upload subdirectories exist
for sub in (
    os.path.join("DramaCreate", "videos"),
    os.path.join("DramaCreate", "images"),
    os.path.join("DramaCreate", "exports"),
    os.path.join("DramaCreate", "clips"),
    os.path.join("AdMake", "Source"),
    os.path.join("AdMake", "Parts"),
    os.path.join("AdMake", "Export"),
    os.path.join("AdMake", "ai_tmp"),
):
    os.makedirs(os.path.join(settings.UPLOAD_DIR, sub), exist_ok=True)

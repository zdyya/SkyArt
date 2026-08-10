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
    UPLOAD_DIR: str = "./uploads"

    # AI API (填入真实 Key 后生效)
    HAPPYHORSE_API_KEY: str = ""
    HAPPYHORSE_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"
    QWEN_API_KEY: str = ""
    QWEN_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"

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

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


async def verify_password_async(plain: str, hashed: str) -> bool:
    """给事件循环里的接口用，别直接调上面那个同步版。

    bcrypt 是故意设计得慢的，闲时约 0.3 秒。它是纯 CPU 计算，同步调用会把整个
    事件循环钉住——2026-09-18 机器上跑着 40 个 ffmpeg 时，一次登录要 12.5 秒，
    这 12.5 秒里后端一个请求都不处理，界面上表现为系统自检、任务轮询全部卡死。
    丢到线程里算，事件循环照常收发。
    """
    return await asyncio.to_thread(pwd_context.verify, plain, hashed)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


async def hash_password_async(plain: str) -> str:
    """注册时用。理由同 verify_password_async。"""
    return await asyncio.to_thread(pwd_context.hash, plain)


def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return jwt.encode({"sub": subject, "exp": expire}, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    """Return username (sub) or None if invalid."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

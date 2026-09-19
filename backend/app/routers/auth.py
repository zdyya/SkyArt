from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.user import User
from app.schemas.auth import RegisterRequest, LoginRequest, TokenResponse, UserOut
from app.core.security import (hash_password_async, verify_password_async,
                               create_access_token)
from app.core.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Check duplicate username
    res = await db.execute(select(User).where(User.username == body.username))
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")
    if body.email:
        res2 = await db.execute(select(User).where(User.email == body.email))
        if res2.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="邮箱已被注册")

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=await hash_password_async(body.password),
    )
    db.add(user)
    await db.flush()  # get user.id
    token = create_access_token(user.username)
    return TokenResponse(access_token=token, user_id=user.id, username=user.username)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.username == body.username))
    user = res.scalar_one_or_none()
    if not user or not await verify_password_async(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(user.username)
    return TokenResponse(access_token=token, user_id=user.id, username=user.username)


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/refresh", response_model=TokenResponse)
async def refresh(current_user: User = Depends(get_current_user)):
    """Issue a new token with renewed 30-day expiry (rolling refresh)."""
    token = create_access_token(current_user.username)
    return TokenResponse(access_token=token, user_id=current_user.id, username=current_user.username)

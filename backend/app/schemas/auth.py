from pydantic import BaseModel, Field
from datetime import datetime


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    role: str = "admin"
    created_at: datetime

    model_config = {"from_attributes": True}

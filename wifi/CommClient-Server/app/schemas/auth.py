"""
Auth request/response schemas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=6, max_length=128)
    avatar_url: str | None = None
    bio: str | None = Field(None, max_length=500)


class ChangePasswordRequest(BaseModel):
    """User changes their own password — must prove they know the old one."""
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password:     str = Field(..., min_length=6, max_length=128)


class AdminResetPasswordRequest(BaseModel):
    """Admin sets a user's password without knowing the old one."""
    new_password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    device_name: str | None = Field(None, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class AuthResponse(BaseModel):
    user: "UserBrief"
    tokens: TokenResponse


class UserBrief(BaseModel):
    id: str
    username: str
    share_code: str
    display_name: str
    avatar_url: str | None
    status: str

    class Config:
        from_attributes = True

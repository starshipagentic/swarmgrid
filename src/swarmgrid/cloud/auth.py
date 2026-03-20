"""GitHub OAuth + JWT session management for SwarmGrid cloud."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import SessionLocal, User, utc_now

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "swarmgrid-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "72"))

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

_bearer_scheme = HTTPBearer(auto_error=False)


def github_login_url(redirect_uri: str) -> str:
    return (
        f"{GITHUB_AUTHORIZE_URL}"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=read:user"
    )


async def github_callback(code: str) -> dict:
    """Exchange OAuth code for GitHub user info. Returns dict with github_id, login, name, avatar_url."""
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="GitHub OAuth failed: no access_token")

        user_resp = await client.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_resp.raise_for_status()
        gh = user_resp.json()

    return {
        "github_id": gh["id"],
        "login": gh["login"],
        "name": gh.get("name") or gh["login"],
        "avatar_url": gh.get("avatar_url", ""),
    }


def upsert_user(gh: dict) -> User:
    """Create or update User row from GitHub profile. Returns the User."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.github_id == gh["github_id"]).first()
        if user:
            user.github_login = gh["login"]
            user.display_name = gh["name"]
            user.avatar_url = gh["avatar_url"]
            user.last_login_at = utc_now()
        else:
            user = User(
                github_id=gh["github_id"],
                github_login=gh["login"],
                display_name=gh["name"],
                avatar_url=gh["avatar_url"],
            )
            db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def create_jwt(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "github_login": user.github_login,
        "exp": datetime.now(UTC) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> User:
    """FastAPI dependency — extracts and validates JWT, returns User."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    claims = decode_jwt(credentials.credentials)
    user_id = int(claims["sub"])
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    finally:
        db.close()

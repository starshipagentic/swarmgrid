"""SwarmGrid Cloud — FastAPI application.

Run locally:
    uvicorn swarmgrid.cloud.app:app --reload

Environment variables:
    DATABASE_URL          — SQLAlchemy URL (default: sqlite:///./swarmgrid.db)
    GITHUB_CLIENT_ID      — GitHub OAuth app client ID
    GITHUB_CLIENT_SECRET  — GitHub OAuth app client secret
    JWT_SECRET            — Secret for signing JWT tokens
    JWT_EXPIRY_HOURS      — Token expiry (default: 72)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import create_jwt, github_callback, github_login_url, upsert_user
from .db import create_tables
from . import api_boards, api_teams, api_templates, api_edge, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    yield


app = FastAPI(
    title="SwarmGrid Cloud",
    description="Cloud orchestration layer for SwarmGrid — edge-powered agent platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount routers ──────────────────────────────────────────────────────

app.include_router(api_boards.router)
app.include_router(api_teams.router)
app.include_router(api_templates.router)
app.include_router(api_edge.router)
app.include_router(ws.router)


# ── Auth endpoints ─────────────────────────────────────────────────────

@app.get("/auth/login")
def login(request: Request):
    """Redirect to GitHub OAuth authorization page."""
    redirect_uri = str(request.url_for("auth_callback"))
    return RedirectResponse(github_login_url(redirect_uri))


@app.get("/auth/callback", name="auth_callback")
async def callback(code: str):
    """GitHub OAuth callback — exchanges code for user info, creates JWT."""
    gh = await github_callback(code)
    user = upsert_user(gh)
    token = create_jwt(user)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "github_login": user.github_login,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
        },
    }


# ── Health ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "swarmgrid-cloud"}


@app.get("/")
def root():
    return {"message": "SwarmGrid Cloud API", "docs": "/docs"}

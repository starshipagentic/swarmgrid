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
import os


from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import create_jwt, github_callback, github_login_url, upsert_user
from .db import create_tables, SessionLocal, Template
from . import api_boards, api_teams, api_templates, api_edge, ws


GLOBAL_TEMPLATES = [
    {
        "name": "/solve",
        "description": "General-purpose bug/task solver — reads the codebase, implements the fix, runs tests.",
        "prompt_template": "Solve ticket {issue_key}: {summary}. Read the codebase, implement the fix, run tests.",
        "recommended_transition_on_launch": "In Progress",
        "recommended_transition_on_success": "Review",
        "recommended_transition_on_failure": "Blocked",
    },
    {
        "name": "/prd2epic",
        "description": "Convert a PRD document into an Epic with Stories and acceptance criteria.",
        "prompt_template": "Convert the PRD in {issue_key} into an Epic. Break it into Stories with clear acceptance criteria and estimates.",
        "recommended_transition_on_success": "Review",
    },
    {
        "name": "/epic2stories",
        "description": "Break an Epic into implementable Stories with subtasks.",
        "prompt_template": "Break Epic {issue_key}: {summary} into Stories. Each Story should be independently implementable with subtasks, AC, and estimates.",
        "recommended_transition_on_success": "Review",
    },
    {
        "name": "/testgen",
        "description": "Generate comprehensive tests for uncovered code changes.",
        "prompt_template": "Generate comprehensive tests for the changes described in {issue_key}: {summary}. Cover edge cases, error paths, and integration scenarios.",
        "recommended_transition_on_success": "Review",
    },
    {
        "name": "/migrate",
        "description": "Implement database migration from a ticket description.",
        "prompt_template": "Implement the database migration described in {issue_key}: {summary}. Generate migration files, update models, and verify with tests.",
        "recommended_transition_on_launch": "In Progress",
        "recommended_transition_on_success": "Review",
        "recommended_transition_on_failure": "Blocked",
    },
]


def _seed_global_templates():
    """Insert global templates if they don't already exist."""
    db = SessionLocal()
    try:
        existing = {t.name for t in db.query(Template).filter(
            Template.team_id.is_(None), Template.board_id.is_(None)
        ).all()}
        for tpl in GLOBAL_TEMPLATES:
            if tpl["name"] not in existing:
                db.add(Template(
                    name=tpl["name"],
                    description=tpl.get("description", ""),
                    prompt_template=tpl.get("prompt_template", ""),
                    recommended_transition_on_launch=tpl.get("recommended_transition_on_launch"),
                    recommended_transition_on_success=tpl.get("recommended_transition_on_success"),
                    recommended_transition_on_failure=tpl.get("recommended_transition_on_failure"),
                    created_by=None,  # system-seeded
                ))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    _seed_global_templates()
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

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://swarmgrid.org")
OAUTH_CALLBACK_URL = os.environ.get("OAUTH_CALLBACK_URL", "https://swarmgrid-api.fly.dev/api/auth/github/callback")


@app.get("/api/auth/github")
@app.get("/auth/login")
def login(request: Request):
    """Redirect to GitHub OAuth authorization page."""
    return RedirectResponse(github_login_url(OAUTH_CALLBACK_URL))


@app.get("/auth/callback", name="auth_callback")
@app.get("/api/auth/github/callback")
async def callback(code: str):
    """GitHub OAuth callback — exchanges code for user info, redirects to dashboard with token in URL."""
    gh = await github_callback(code)
    user = upsert_user(gh)
    token = create_jwt(user)
    return RedirectResponse(f"{FRONTEND_URL}/dashboard.html?token={token}", status_code=302)


@app.get("/api/auth/me")
async def me(request: Request):
    """Return current user from JWT cookie or header."""
    token = request.cookies.get("swarmgrid_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from .auth import decode_jwt
    claims = decode_jwt(token)
    user_id = int(claims["sub"])
    from .db import SessionLocal, User
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return {
            "id": user.id,
            "github_login": user.github_login,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
        }
    finally:
        db.close()


# ── Health ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "swarmgrid-cloud"}


@app.get("/")
def root():
    return {"message": "SwarmGrid Cloud API", "docs": "/docs"}

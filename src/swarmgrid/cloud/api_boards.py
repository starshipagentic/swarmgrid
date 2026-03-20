"""Board + Route CRUD — same API shape as existing /api/snapshot, /api/routes."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .db import Board, Route, SessionLocal, TeamMember, User, AgentSession

router = APIRouter(prefix="/api/boards", tags=["boards"])


# ── Schemas ────────────────────────────────────────────────────────────

class BoardCreate(BaseModel):
    team_id: int
    name: str
    site_url: str = ""
    project_key: str = ""
    jira_board_id: str = ""


class BoardUpdate(BaseModel):
    name: str | None = None
    site_url: str | None = None
    project_key: str | None = None
    jira_board_id: str | None = None


class RouteCreate(BaseModel):
    status: str
    action: str = "claude_default"
    prompt_template: str = ""
    enabled: bool = False


class RouteUpdate(BaseModel):
    prompt_template: str | None = None
    transition_on_launch: str | None = None
    transition_on_success: str | None = None
    transition_on_failure: str | None = None
    allowed_issue_types: list[str] | None = None
    enabled: bool | None = None


# ── Helpers ────────────────────────────────────────────────────────────

def _require_board_access(board_id: int, user: User) -> Board:
    db = SessionLocal()
    try:
        board = db.query(Board).filter(Board.id == board_id).first()
        if not board:
            raise HTTPException(status_code=404, detail="Board not found")
        member = (
            db.query(TeamMember)
            .filter(TeamMember.team_id == board.team_id, TeamMember.user_id == user.id)
            .first()
        )
        if not member:
            raise HTTPException(status_code=403, detail="Not a member of this board's team")
        return board
    finally:
        db.close()


def _route_to_dict(r: Route) -> dict:
    return {
        "id": r.id,
        "board_id": r.board_id,
        "status": r.status,
        "action": r.action,
        "prompt_template": r.prompt_template,
        "enabled": r.enabled,
        "transition_on_launch": r.transition_on_launch,
        "transition_on_success": r.transition_on_success,
        "transition_on_failure": r.transition_on_failure,
        "allowed_issue_types": json.loads(r.allowed_issue_types) if r.allowed_issue_types else [],
    }


def _board_to_dict(b: Board) -> dict:
    return {
        "id": b.id,
        "team_id": b.team_id,
        "name": b.name,
        "site_url": b.site_url,
        "project_key": b.project_key,
        "jira_board_id": b.jira_board_id,
    }


# ── Board CRUD ─────────────────────────────────────────────────────────

@router.get("")
def list_boards(user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        team_ids = [m.team_id for m in db.query(TeamMember).filter(TeamMember.user_id == user.id).all()]
        boards = db.query(Board).filter(Board.team_id.in_(team_ids)).all() if team_ids else []
        return {"boards": [_board_to_dict(b) for b in boards]}
    finally:
        db.close()


@router.post("", status_code=201)
def create_board(body: BoardCreate, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        member = (
            db.query(TeamMember)
            .filter(TeamMember.team_id == body.team_id, TeamMember.user_id == user.id)
            .first()
        )
        if not member:
            raise HTTPException(status_code=403, detail="Not a member of this team")
        board = Board(
            team_id=body.team_id,
            name=body.name,
            site_url=body.site_url,
            project_key=body.project_key,
            jira_board_id=body.jira_board_id,
            created_by=user.id,
        )
        db.add(board)
        db.commit()
        db.refresh(board)
        return {"ok": True, "board": _board_to_dict(board)}
    finally:
        db.close()


@router.get("/{board_id}")
def get_board(board_id: int, user: User = Depends(get_current_user)):
    board = _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        board = db.query(Board).filter(Board.id == board_id).first()
        return {"board": _board_to_dict(board)}
    finally:
        db.close()


@router.put("/{board_id}")
def update_board(board_id: int, body: BoardUpdate, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        board = db.query(Board).filter(Board.id == board_id).first()
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(board, field, value)
        db.commit()
        db.refresh(board)
        return {"ok": True, "board": _board_to_dict(board)}
    finally:
        db.close()


@router.delete("/{board_id}")
def delete_board(board_id: int, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        board = db.query(Board).filter(Board.id == board_id).first()
        db.delete(board)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.get("/{board_id}/snapshot")
def board_snapshot(board_id: int, user: User = Depends(get_current_user)):
    """Return board state in the same shape as the existing /api/snapshot."""
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        board = db.query(Board).filter(Board.id == board_id).first()
        routes = db.query(Route).filter(Route.board_id == board_id).all()
        sessions = (
            db.query(AgentSession)
            .filter(AgentSession.board_id == board_id)
            .order_by(AgentSession.launched_at.desc())
            .limit(100)
            .all()
        )

        # Build columns from routes (same shape as existing webapp)
        columns = []
        for route in routes:
            col_sessions = [s for s in sessions if s.state in ("running", "launching") and _status_matches(s, route)]
            columns.append({
                "status": route.status,
                "tickets": [
                    {
                        "key": s.ticket_key,
                        "summary": s.ticket_summary,
                        "state": s.state,
                        "session_id": s.session_id,
                    }
                    for s in col_sessions
                ],
            })

        return {
            "board": _board_to_dict(board),
            "columns": columns,
            "routes": [_route_to_dict(r) for r in routes],
            "sessions": [
                {
                    "session_id": s.session_id,
                    "ticket_key": s.ticket_key,
                    "state": s.state,
                    "launched_at": s.launched_at.isoformat() if s.launched_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                }
                for s in sessions
            ],
        }
    finally:
        db.close()


def _status_matches(session: AgentSession, route: Route) -> bool:
    """Check if a session belongs to a route's column (by ticket key prefix or other heuristic)."""
    return True  # placeholder — will refine when tickets carry status info


# ── Route CRUD ─────────────────────────────────────────────────────────

@router.get("/{board_id}/routes")
def list_routes(board_id: int, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        routes = db.query(Route).filter(Route.board_id == board_id).all()
        return {"routes": [_route_to_dict(r) for r in routes]}
    finally:
        db.close()


@router.post("/{board_id}/routes", status_code=201)
def create_route(board_id: int, body: RouteCreate, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        existing = db.query(Route).filter(Route.board_id == board_id, Route.status == body.status).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Route for status '{body.status}' already exists")
        route = Route(
            board_id=board_id,
            status=body.status,
            action=body.action,
            prompt_template=body.prompt_template,
            enabled=body.enabled,
        )
        db.add(route)
        db.commit()
        db.refresh(route)
        return {"ok": True, "route": _route_to_dict(route)}
    finally:
        db.close()


@router.put("/{board_id}/routes/{status}")
def update_route(board_id: int, status: str, body: RouteUpdate, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        route = db.query(Route).filter(Route.board_id == board_id, Route.status == status).first()
        if not route:
            raise HTTPException(status_code=404, detail=f"No route for status '{status}'")
        updates = body.model_dump(exclude_none=True)
        if "allowed_issue_types" in updates:
            updates["allowed_issue_types"] = json.dumps(updates["allowed_issue_types"])
        for field, value in updates.items():
            setattr(route, field, value)
        db.commit()
        db.refresh(route)
        return {"ok": True, "route": _route_to_dict(route)}
    finally:
        db.close()


@router.post("/{board_id}/routes/{status}/toggle")
def toggle_route(board_id: int, status: str, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        route = db.query(Route).filter(Route.board_id == board_id, Route.status == status).first()
        if not route:
            raise HTTPException(status_code=404, detail=f"No route for status '{status}'")
        route.enabled = not route.enabled
        db.commit()
        return {"ok": True, "status": status, "enabled": route.enabled}
    finally:
        db.close()


@router.delete("/{board_id}/routes/{status}")
def delete_route(board_id: int, status: str, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        route = db.query(Route).filter(Route.board_id == board_id, Route.status == status).first()
        if not route:
            raise HTTPException(status_code=404, detail=f"No route for status '{status}'")
        db.delete(route)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

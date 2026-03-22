"""Board + Route CRUD — same API shape as existing /api/snapshot, /api/routes."""
from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from .auth import get_current_user
from .crypto import encrypt, decrypt
from .db import Board, BoardMember, Route, SessionLocal, Team, TeamMember, User, AgentSession

router = APIRouter(prefix="/api/boards", tags=["boards"])


# ── Schemas ────────────────────────────────────────────────────────────

class BoardCreate(BaseModel):
    team_id: int | None = None
    name: str = ""
    site_url: str = ""
    project_key: str = ""
    jira_board_id: str = ""
    board_url: str = ""
    board_id: int | None = None
    jira_email: str = ""
    jira_token: str = ""


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
    transition_on_launch: str | None = None
    transition_on_success: str | None = None
    transition_on_failure: str | None = None
    allowed_issue_types: list[str] | None = None


class RouteUpdate(BaseModel):
    action: str | None = None
    prompt_template: str | None = None
    transition_on_launch: str | None = None
    transition_on_success: str | None = None
    transition_on_failure: str | None = None
    allowed_issue_types: list[str] | None = None
    enabled: bool | None = None


class MemberAdd(BaseModel):
    github_login: str


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
        "jira_email": decrypt(b.jira_email) if b.jira_email else "",
        "jira_token": "••••" if b.jira_token else "",
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
        team_id = body.team_id
        # Auto-create a personal team if none provided
        if not team_id:
            membership = db.query(TeamMember).filter(TeamMember.user_id == user.id).first()
            if membership:
                team_id = membership.team_id
            else:
                team = Team(
                    slug=user.github_login,
                    name=f"{user.display_name or user.github_login}'s team",
                    created_by=user.id,
                )
                db.add(team)
                db.flush()
                db.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))
                db.flush()
                team_id = team.id

        member = (
            db.query(TeamMember)
            .filter(TeamMember.team_id == team_id, TeamMember.user_id == user.id)
            .first()
        )
        if not member:
            raise HTTPException(status_code=403, detail="Not a member of this team")

        board_name = body.name or body.project_key or "My Board"
        jira_board_id = body.jira_board_id or str(body.board_id or "")
        board = Board(
            team_id=team_id,
            name=board_name,
            site_url=body.site_url,
            project_key=body.project_key,
            jira_board_id=jira_board_id,
            jira_email=encrypt(body.jira_email),
            jira_token=encrypt(body.jira_token),
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
async def board_snapshot(board_id: int, user: User = Depends(get_current_user)):
    """Return board state — fetches live data from Jira if credentials available."""
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

        # Try to fetch live Jira data if credentials are available
        jira_columns = []
        jira_issues = []
        if board.jira_email and board.jira_token and board.jira_board_id:
            try:
                jira_columns, jira_issues = await _fetch_jira_board(board)
            except Exception as e:
                logger.warning("Jira fetch failed for board %s: %s", board_id, e)

        # Build columns: use Jira columns if available, otherwise from routes
        columns = []
        if jira_columns:
            for col in jira_columns:
                col_name = col.get("name", "")
                col_statuses = set(col.get("statuses", [col_name]))
                col_issues = [i for i in jira_issues if i.get("status") in col_statuses]
                route = next((r for r in routes if r.status == col_name), None)
                # Build set of ticket keys with active sessions
                active_sessions = {s.ticket_key: s.state for s in sessions if s.state in ("pending", "launching", "running")}

                columns.append({
                    "name": col_name,
                    "status": col_name,
                    "count": len(col_issues),
                    "armed": route.enabled if route else False,
                    "tickets": [
                        {
                            "key": i["key"],
                            "summary": i.get("summary", ""),
                            "status_name": i.get("status", ""),
                            "assignee": i.get("assignee"),
                            "issue_type": i.get("issue_type", ""),
                            "agent_status": active_sessions.get(i["key"]),
                        }
                        for i in col_issues
                    ],
                })
        else:
            for route in routes:
                columns.append({"name": route.status, "status": route.status, "count": 0, "armed": route.enabled, "tickets": []})

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


async def _fetch_jira_board(board: Board) -> tuple[list, list]:
    """Fetch board columns and issues from Jira API."""
    auth = (decrypt(board.jira_email), decrypt(board.jira_token))
    base = board.site_url.rstrip("/")

    async with httpx.AsyncClient(timeout=15) as client:
        # Fetch board columns
        config_resp = await client.get(
            f"{base}/rest/agile/1.0/board/{board.jira_board_id}/configuration",
            auth=auth,
        )
        config_resp.raise_for_status()
        config_data = config_resp.json()
        raw_columns = config_data.get("columnConfig", {}).get("columns", [])

        # Get status name mapping
        status_map = {}
        try:
            status_resp = await client.get(
                f"{base}/rest/api/3/project/{board.project_key}/statuses",
                auth=auth,
            )
            if status_resp.is_success:
                for block in status_resp.json():
                    for s in block.get("statuses", []):
                        sid = str(s.get("id", ""))
                        sname = s.get("name", "")
                        if sid and sname:
                            status_map[sid] = sname
        except Exception:
            pass

        columns = []
        all_status_names = set()
        for col in raw_columns:
            col_name = col.get("name", "")
            statuses = []
            for st in col.get("statuses", []):
                sid = str(st.get("id", ""))
                sname = status_map.get(sid, sid)
                statuses.append(sname)
                all_status_names.add(sname)
            columns.append({"name": col_name, "statuses": statuses})

        # Fetch issues on the board
        issues = []
        if all_status_names:
            quoted = ", ".join(f'"{s}"' for s in all_status_names)
            jql = f"project = {board.project_key} AND status IN ({quoted}) ORDER BY updated DESC"
            search_resp = await client.post(
                f"{base}/rest/api/3/search/jql",
                auth=auth,
                json={"jql": jql, "fields": ["summary", "status", "assignee", "issuetype"], "maxResults": 50},
            )
            if search_resp.is_success:
                for item in search_resp.json().get("issues", []):
                    fields = item.get("fields", {})
                    assignee = fields.get("assignee")
                    issues.append({
                        "key": item["key"],
                        "summary": fields.get("summary", ""),
                        "status": fields.get("status", {}).get("name", ""),
                        "assignee": assignee.get("displayName") if assignee else None,
                        "issue_type": fields.get("issuetype", {}).get("name", ""),
                    })

        # Map issues to columns by status name
        flat_columns = []
        for col in columns:
            flat_columns.append({
                "name": col["name"],
                "statuses": col["statuses"],
            })

        # Return columns with their status names, and all issues with their status
        return flat_columns, issues


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
            transition_on_launch=body.transition_on_launch,
            transition_on_success=body.transition_on_success,
            transition_on_failure=body.transition_on_failure,
            allowed_issue_types=json.dumps(body.allowed_issue_types) if body.allowed_issue_types else "",
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


# ── Board Members (GitHub usernames) ──────────────────────────────────

@router.get("/{board_id}/members")
def list_board_members(board_id: int, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        members = db.query(BoardMember).filter(BoardMember.board_id == board_id).all()
        return {
            "members": [
                {
                    "id": m.id,
                    "github_login": m.github_login,
                    "added_at": m.added_at.isoformat() if m.added_at else None,
                }
                for m in members
            ]
        }
    finally:
        db.close()


@router.post("/{board_id}/members", status_code=201)
def add_board_member(board_id: int, body: MemberAdd, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    login = body.github_login.strip().lstrip("@").lower()
    if not login:
        raise HTTPException(status_code=400, detail="github_login is required")
    db = SessionLocal()
    try:
        existing = (
            db.query(BoardMember)
            .filter(BoardMember.board_id == board_id, BoardMember.github_login == login)
            .first()
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"'{login}' is already a member of this board")
        member = BoardMember(
            board_id=board_id,
            github_login=login,
            added_by=user.id,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return {
            "ok": True,
            "member": {
                "id": member.id,
                "github_login": member.github_login,
                "added_at": member.added_at.isoformat() if member.added_at else None,
            },
        }
    finally:
        db.close()


@router.delete("/{board_id}/members/{github_login}")
def remove_board_member(board_id: int, github_login: str, user: User = Depends(get_current_user)):
    _require_board_access(board_id, user)
    db = SessionLocal()
    try:
        member = (
            db.query(BoardMember)
            .filter(BoardMember.board_id == board_id, BoardMember.github_login == github_login.lower())
            .first()
        )
        if not member:
            raise HTTPException(status_code=404, detail=f"'{github_login}' is not a member of this board")
        db.delete(member)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

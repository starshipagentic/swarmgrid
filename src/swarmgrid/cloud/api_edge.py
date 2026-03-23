"""Edge node registration, status reporting, and command dispatch."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .crypto import decrypt, encrypt
from .db import AgentSession, Board, BoardMember, EdgeNode, SessionLocal, TeamMember, User, utc_now
from .heartbeat_coordinator import assign_heartbeat_if_needed

router = APIRouter(prefix="/api/edge", tags=["edge"])


# ── Schemas ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    ssh_connect: str  # phonebook agent connect string (cloud-facing)
    frontdesk_connect: str = ""  # front desk agent connect string (team-facing)
    hostname: str = ""
    os: str = ""


class HeartbeatReport(BaseModel):
    board_id: int
    tickets_found: list[dict] = []
    sessions_launched: list[dict] = []


class StatusReport(BaseModel):
    sessions: list[dict] = []


class CompletedReport(BaseModel):
    session_id: str
    result: str = "success"
    output: str = ""


class SessionInfo(BaseModel):
    id: str
    state: str
    output_lines: int = 0


# ── Cloud public key (no auth — agents need this during setup) ─────────

@router.get("/cloud-public-key")
def cloud_public_key():
    """Return the cloud server's SSH public key.

    Edge agents use this to populate --authorized-keys so only the
    cloud (and explicitly added teammates) can SSH into sessions.
    """
    pub_path = Path("/data/.ssh/id_ed25519.pub")
    if not pub_path.exists():
        raise HTTPException(status_code=503, detail="Cloud SSH key not yet initialized")
    public_key = pub_path.read_text().strip()
    return {"public_key": public_key}


# ── Authorized keys (auth required) ───────────────────────────────────

@router.get("/authorized-keys")
def get_authorized_keys(user: User = Depends(get_current_user)):
    """Return SSH authorized_keys + github_users for this user's agent.

    The agent calls this on startup to build ~/.swarmgrid/authorized_keys
    so upterm restricts connections to the cloud key and teammates only.
    """
    from .db import TeamMember

    authorized_keys: list[str] = []
    github_users: list[str] = []

    # 1. Always include the cloud's own public key
    cloud_pub_path = Path("/data/.ssh/id_ed25519.pub")
    try:
        cloud_key = cloud_pub_path.read_text().strip()
        if cloud_key:
            authorized_keys.append(cloud_key)
    except FileNotFoundError:
        pass  # Cloud key not yet generated — shouldn't happen in prod

    # 2. Collect github_logins for all teammates on this user's teams
    db = SessionLocal()
    try:
        user_team_ids = [
            m.team_id
            for m in db.query(TeamMember).filter(TeamMember.user_id == user.id).all()
        ]
        if user_team_ids:
            teammates = (
                db.query(User)
                .join(TeamMember, TeamMember.user_id == User.id)
                .filter(
                    TeamMember.team_id.in_(user_team_ids),
                    User.id != user.id,
                )
                .all()
            )
            for t in teammates:
                if t.github_login and t.github_login not in github_users:
                    github_users.append(t.github_login)

        # Future: when User model has ssh_public_key column, collect those
        # into authorized_keys as well.

    finally:
        db.close()

    return {
        "authorized_keys": authorized_keys,
        "github_users": github_users,
    }


# ── Registration ───────────────────────────────────────────────────────

@router.post("/register")
def register_edge(body: RegisterRequest, user: User = Depends(get_current_user)):
    """Register or re-register an edge node. Idempotent — updates connect string on restart."""
    db = SessionLocal()
    try:
        # Find existing node for this user+hostname, or create new
        node = (
            db.query(EdgeNode)
            .filter(EdgeNode.owner_id == user.id, EdgeNode.hostname == body.hostname)
            .first()
        )
        if node:
            node.ssh_connect = encrypt(body.ssh_connect)
            if body.frontdesk_connect:
                node.frontdesk_connect = encrypt(body.frontdesk_connect)
            node.os_name = body.os
            node.online = True
            node.last_seen_at = utc_now()
        else:
            node = EdgeNode(
                owner_id=user.id,
                hostname=body.hostname,
                os_name=body.os,
                ssh_connect=encrypt(body.ssh_connect),
                frontdesk_connect=encrypt(body.frontdesk_connect) if body.frontdesk_connect else "",
                online=True,
            )
            db.add(node)
        db.commit()
        db.refresh(node)

        # Check if this node should become primary heartbeat for any boards
        assign_heartbeat_if_needed(db, node)

        return {
            "ok": True,
            "edge_id": node.id,
            "hostname": node.hostname,
        }
    finally:
        db.close()


@router.post("/heartbeat")
def edge_heartbeat(body: HeartbeatReport, user: User = Depends(get_current_user)):
    """Edge reports heartbeat results — tickets found and sessions launched."""
    db = SessionLocal()
    try:
        node = db.query(EdgeNode).filter(EdgeNode.owner_id == user.id, EdgeNode.online == True).first()
        if not node:
            raise HTTPException(status_code=404, detail="No online edge node found for this user")
        node.last_seen_at = utc_now()

        # Record any new sessions that were launched
        for s in body.sessions_launched:
            session_id = s.get("session_id", "")
            if not session_id:
                continue
            existing = db.query(AgentSession).filter(AgentSession.session_id == session_id).first()
            if not existing:
                session = AgentSession(
                    session_id=session_id,
                    board_id=body.board_id,
                    edge_node_id=node.id,
                    ticket_key=s.get("ticket_key", ""),
                    ticket_summary=s.get("ticket_summary", ""),
                    prompt=encrypt(s.get("prompt", "")),
                    state="running",
                )
                db.add(session)

        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/status")
def edge_status(body: StatusReport, user: User = Depends(get_current_user)):
    """Edge reports current session states."""
    db = SessionLocal()
    try:
        node = db.query(EdgeNode).filter(EdgeNode.owner_id == user.id, EdgeNode.online == True).first()
        if not node:
            raise HTTPException(status_code=404, detail="No online edge node found")
        node.last_seen_at = utc_now()

        for s in body.sessions:
            session = db.query(AgentSession).filter(AgentSession.session_id == s.get("id", "")).first()
            if session:
                session.state = s.get("state", session.state)
                if "output" in s:
                    session.output_snapshot = encrypt(s["output"])

        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/completed")
def edge_completed(body: CompletedReport, user: User = Depends(get_current_user)):
    """Edge reports a session has completed."""
    db = SessionLocal()
    try:
        session = db.query(AgentSession).filter(AgentSession.session_id == body.session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session.state = "completed" if body.result == "success" else "failed"
        session.result = body.result
        session.output_snapshot = encrypt(body.output)
        session.completed_at = utc_now()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/offline")
def edge_offline(user: User = Depends(get_current_user)):
    """Graceful shutdown — mark all edge nodes for this user as offline."""
    db = SessionLocal()
    try:
        nodes = db.query(EdgeNode).filter(EdgeNode.owner_id == user.id, EdgeNode.online == True).all()
        for node in nodes:
            node.online = False
        db.commit()
        return {"ok": True, "nodes_marked_offline": len(nodes)}
    finally:
        db.close()


# ── Cloud-facing queries ───────────────────────────────────────────────

@router.get("/nodes")
def list_edge_nodes(user: User = Depends(get_current_user)):
    """List edge nodes visible to the current user (own + teammates')."""
    db = SessionLocal()
    try:
        # Get all team IDs for this user
        team_ids = [m.team_id for m in db.query(TeamMember).filter(TeamMember.user_id == user.id).all()]
        if not team_ids:
            # Just show own nodes
            nodes = db.query(EdgeNode).filter(EdgeNode.owner_id == user.id).all()
        else:
            # Show nodes for all teammates
            teammate_ids = [
                m.user_id
                for m in db.query(TeamMember).filter(TeamMember.team_id.in_(team_ids)).all()
            ]
            nodes = db.query(EdgeNode).filter(EdgeNode.owner_id.in_(teammate_ids)).all()

        return {
            "nodes": [
                {
                    "id": n.id,
                    "owner_id": n.owner_id,
                    "hostname": n.hostname,
                    "os": n.os_name,
                    "online": n.online,
                    "has_frontdesk": bool(n.frontdesk_connect),
                    "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
                }
                for n in nodes
            ]
        }
    finally:
        db.close()


@router.get("/nodes/{node_id}/frontdesk")
def get_frontdesk_connect(node_id: int, user: User = Depends(get_current_user)):
    """Return a teammate's front desk connect string for SSH discovery.

    The caller must be on the same team as the node owner. The front desk
    connect string lets them SSH in as their GitHub user to discover
    real session connect strings.
    """
    db = SessionLocal()
    try:
        node = db.query(EdgeNode).filter(EdgeNode.id == node_id).first()
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")
        if not node.frontdesk_connect:
            raise HTTPException(status_code=404, detail="Node has no front desk agent")

        # Verify caller is on the same team as the node owner
        caller_team_ids = set(
            m.team_id for m in db.query(TeamMember).filter(TeamMember.user_id == user.id).all()
        )
        owner_team_ids = set(
            m.team_id for m in db.query(TeamMember).filter(TeamMember.user_id == node.owner_id).all()
        )
        if not caller_team_ids & owner_team_ids:
            raise HTTPException(status_code=403, detail="Not on the same team as this node's owner")

        return {
            "ok": True,
            "frontdesk_connect": decrypt(node.frontdesk_connect),
            "hostname": node.hostname,
            "owner_id": node.owner_id,
        }
    finally:
        db.close()


@router.post("/command/{ticket_key}")
def send_edge_command(ticket_key: str, user: User = Depends(get_current_user)):
    """Send a command to the user's edge node via SSH relay.

    Currently supports: attach (open iTerm2 for a ticket's session).
    The cloud is just a teammate — it SSHs into the edge and sends
    the command through the force-command CGI handler.
    """
    db = SessionLocal()
    try:
        # Find the user's online edge node
        node = db.query(EdgeNode).filter(
            EdgeNode.owner_id == user.id,
            EdgeNode.online == True,
        ).first()
        if not node:
            raise HTTPException(status_code=404, detail="No online edge node")
        ssh = decrypt(node.ssh_connect) if node.ssh_connect else ""
        if not ssh:
            raise HTTPException(status_code=400, detail="Edge node has no SSH connect string")

        from .relay import attach_session
        result = attach_session(ssh, ticket_key=ticket_key)
        return result
    finally:
        db.close()


@router.post("/capture/{session_id}")
def capture_session_output(session_id: str, user: User = Depends(get_current_user)):
    """Capture terminal output from a session via SSH relay."""
    db = SessionLocal()
    try:
        node = db.query(EdgeNode).filter(
            EdgeNode.owner_id == user.id,
            EdgeNode.online == True,
        ).first()
        if not node:
            raise HTTPException(status_code=404, detail="No online edge node with SSH")
        ssh = decrypt(node.ssh_connect) if node.ssh_connect else ""
        if not ssh:
            raise HTTPException(status_code=404, detail="No online edge node with SSH")

        from .relay import capture_output
        result = capture_output(ssh, session_id)
        return result
    finally:
        db.close()


@router.get("/github-users")
def get_github_users(user: User = Depends(get_current_user)):
    """Return all unique github_logins across all boards owned by the current user.

    Used by the agent to build the --github-user list for upterm sessions.
    """
    db = SessionLocal()
    try:
        # Find all teams the user belongs to
        team_ids = [m.team_id for m in db.query(TeamMember).filter(TeamMember.user_id == user.id).all()]
        if not team_ids:
            return {"github_users": []}

        # Find all boards in those teams
        board_ids = [b.id for b in db.query(Board).filter(Board.team_id.in_(team_ids)).all()]
        if not board_ids:
            return {"github_users": []}

        # Collect unique github_logins from board members
        members = db.query(BoardMember).filter(BoardMember.board_id.in_(board_ids)).all()
        logins = sorted(set(m.github_login for m in members))
        return {"github_users": logins}
    finally:
        db.close()


@router.get("/team-config")
def get_team_config(user: User = Depends(get_current_user)):
    """Return board→github_users mapping for the agent to cache locally.

    The front desk worker uses this to check board-level access:
    "Is karthik allowed to see LMSV3-857?" → check LMSV3 board's github_users.

    Also returns the flat github_users list for the --github-user flag.
    """
    db = SessionLocal()
    try:
        team_ids = [m.team_id for m in db.query(TeamMember).filter(TeamMember.user_id == user.id).all()]
        if not team_ids:
            return {"boards": {}, "github_users": []}

        boards_data = {}
        all_users: set[str] = set()

        boards = db.query(Board).filter(Board.team_id.in_(team_ids)).all()
        for b in boards:
            members = db.query(BoardMember).filter(BoardMember.board_id == b.id).all()
            board_users = sorted(set(m.github_login for m in members))
            # Use project_key as the board prefix (e.g., "LMSV3")
            key = b.project_key or b.name
            boards_data[key] = {
                "board_id": b.id,
                "github_users": board_users,
            }
            all_users.update(board_users)

        return {
            "boards": boards_data,
            "github_users": sorted(all_users),
        }
    finally:
        db.close()

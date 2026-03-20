"""Edge node registration, status reporting, and command dispatch."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .db import AgentSession, EdgeNode, SessionLocal, User, utc_now
from .heartbeat_coordinator import assign_heartbeat_if_needed

router = APIRouter(prefix="/api/edge", tags=["edge"])


# ── Schemas ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    ssh_connect: str
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
            node.ssh_connect = body.ssh_connect
            node.os_name = body.os
            node.online = True
            node.last_seen_at = utc_now()
        else:
            node = EdgeNode(
                owner_id=user.id,
                hostname=body.hostname,
                os_name=body.os,
                ssh_connect=body.ssh_connect,
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
                    prompt=s.get("prompt", ""),
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
                    session.output_snapshot = s["output"]

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
        session.output_snapshot = body.output
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
        from .db import TeamMember
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
                    "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
                }
                for n in nodes
            ]
        }
    finally:
        db.close()

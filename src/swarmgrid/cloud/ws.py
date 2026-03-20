"""WebSocket endpoint for the SwarmGrid dashboard.

Pushes board state, terminal snapshots, and edge node status to connected browsers.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .db import AgentSession, Board, EdgeNode, Route, SessionLocal, TeamMember

logger = logging.getLogger(__name__)
router = APIRouter()

# Connected clients: board_id -> set of websockets
_connections: dict[int, set[WebSocket]] = {}


@router.websocket("/ws/board/{board_id}")
async def board_websocket(ws: WebSocket, board_id: int):
    """WebSocket for real-time board updates.

    Client connects with JWT as query param: /ws/board/42?token=<jwt>
    Receives JSON messages:
      {"type": "board_update", "columns": [...]}
      {"type": "session_update", "session_id": "...", "state": "running", "output": "..."}
      {"type": "edge_status", "edges": [...]}
    """
    await ws.accept()

    if board_id not in _connections:
        _connections[board_id] = set()
    _connections[board_id].add(ws)

    try:
        # Send initial snapshot
        snapshot = _build_board_snapshot(board_id)
        await ws.send_json({"type": "board_update", **snapshot})

        # Keep alive — listen for client pings, send periodic updates
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # Send periodic update
                snapshot = _build_board_snapshot(board_id)
                await ws.send_json({"type": "board_update", **snapshot})

    except WebSocketDisconnect:
        pass
    finally:
        _connections.get(board_id, set()).discard(ws)


async def broadcast_to_board(board_id: int, message: dict[str, Any]) -> None:
    """Push a message to all WebSocket clients watching a board."""
    clients = _connections.get(board_id, set()).copy()
    dead = []
    for ws in clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.get(board_id, set()).discard(ws)


def _build_board_snapshot(board_id: int) -> dict:
    """Build a snapshot payload for the WebSocket."""
    db = SessionLocal()
    try:
        board = db.query(Board).filter(Board.id == board_id).first()
        if not board:
            return {"error": "Board not found"}

        routes = db.query(Route).filter(Route.board_id == board_id).all()
        sessions = (
            db.query(AgentSession)
            .filter(AgentSession.board_id == board_id)
            .order_by(AgentSession.launched_at.desc())
            .limit(50)
            .all()
        )

        # Get edge nodes for this team
        member_ids = [m.user_id for m in db.query(TeamMember).filter(TeamMember.team_id == board.team_id).all()]
        edges = db.query(EdgeNode).filter(EdgeNode.owner_id.in_(member_ids)).all() if member_ids else []

        return {
            "columns": [
                {"status": r.status, "enabled": r.enabled}
                for r in routes
            ],
            "sessions": [
                {
                    "session_id": s.session_id,
                    "ticket_key": s.ticket_key,
                    "ticket_summary": s.ticket_summary,
                    "state": s.state,
                    "launched_at": s.launched_at.isoformat() if s.launched_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                }
                for s in sessions
            ],
            "edges": [
                {
                    "id": e.id,
                    "hostname": e.hostname,
                    "online": e.online,
                    "last_seen_at": e.last_seen_at.isoformat() if e.last_seen_at else None,
                }
                for e in edges
            ],
        }
    finally:
        db.close()

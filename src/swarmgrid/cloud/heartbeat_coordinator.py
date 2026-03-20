"""Heartbeat coordinator — assigns primary heartbeat node per board, handles failover."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from .db import Board, EdgeNode, HeartbeatAssignment, TeamMember, utc_now

logger = logging.getLogger(__name__)

# If an edge node hasn't been seen in this many seconds, consider it offline
OFFLINE_THRESHOLD_SECONDS = 120


def assign_heartbeat_if_needed(db: Session, node: EdgeNode) -> None:
    """Check all boards this node's owner belongs to.
    If any board lacks a primary heartbeat, assign this node.
    """
    team_ids = [m.team_id for m in db.query(TeamMember).filter(TeamMember.user_id == node.owner_id).all()]
    if not team_ids:
        return

    boards = db.query(Board).filter(Board.team_id.in_(team_ids)).all()
    for board in boards:
        assignment = db.query(HeartbeatAssignment).filter(HeartbeatAssignment.board_id == board.id).first()
        if not assignment:
            # No primary — assign this node
            assignment = HeartbeatAssignment(board_id=board.id, primary_edge_id=node.id)
            db.add(assignment)
            logger.info("Assigned edge %s (%s) as primary heartbeat for board %d", node.id, node.hostname, board.id)
        else:
            # Check if current primary is still online
            primary = db.query(EdgeNode).filter(EdgeNode.id == assignment.primary_edge_id).first()
            if primary and not _is_node_alive(primary):
                # Failover to this node
                assignment.primary_edge_id = node.id
                assignment.assigned_at = utc_now()
                logger.info(
                    "Failover: edge %s (%s) promoted to primary heartbeat for board %d (previous: %s)",
                    node.id, node.hostname, board.id, primary.hostname,
                )

    db.commit()


def check_failovers(db: Session) -> list[dict]:
    """Scan all heartbeat assignments. If the primary is offline, promote another node.

    Returns a list of failover events for logging/notification.
    """
    events = []
    assignments = db.query(HeartbeatAssignment).all()

    for assignment in assignments:
        primary = db.query(EdgeNode).filter(EdgeNode.id == assignment.primary_edge_id).first()
        if primary and _is_node_alive(primary):
            continue

        # Primary is dead or missing — find a replacement
        board = db.query(Board).filter(Board.id == assignment.board_id).first()
        if not board:
            continue

        team_ids = [board.team_id]
        member_ids = [m.user_id for m in db.query(TeamMember).filter(TeamMember.team_id.in_(team_ids)).all()]
        candidates = (
            db.query(EdgeNode)
            .filter(EdgeNode.owner_id.in_(member_ids), EdgeNode.online == True, EdgeNode.id != assignment.primary_edge_id)
            .all()
        )

        alive = [c for c in candidates if _is_node_alive(c)]
        if alive:
            new_primary = alive[0]
            old_hostname = primary.hostname if primary else "unknown"
            assignment.primary_edge_id = new_primary.id
            assignment.assigned_at = utc_now()
            events.append({
                "board_id": board.id,
                "old_primary": old_hostname,
                "new_primary": new_primary.hostname,
            })
            logger.info(
                "Failover: %s -> %s for board %d",
                old_hostname, new_primary.hostname, board.id,
            )
        else:
            logger.warning("Board %d has no online edge nodes — heartbeat paused", board.id)

    if events:
        db.commit()
    return events


def get_primary_for_board(db: Session, board_id: int) -> EdgeNode | None:
    """Get the primary heartbeat edge node for a board."""
    assignment = db.query(HeartbeatAssignment).filter(HeartbeatAssignment.board_id == board_id).first()
    if not assignment:
        return None
    node = db.query(EdgeNode).filter(EdgeNode.id == assignment.primary_edge_id).first()
    if node and _is_node_alive(node):
        return node
    return None


def _is_node_alive(node: EdgeNode) -> bool:
    if not node.online:
        return False
    if not node.last_seen_at:
        return False
    threshold = datetime.now(UTC) - timedelta(seconds=OFFLINE_THRESHOLD_SECONDS)
    return node.last_seen_at.replace(tzinfo=UTC) >= threshold

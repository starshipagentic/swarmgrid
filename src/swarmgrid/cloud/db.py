"""SQLAlchemy models for SwarmGrid cloud.

Uses SQLite for local dev, Postgres for production.
Switch via DATABASE_URL env var (defaults to sqlite:///./swarmgrid.db).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./swarmgrid.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


# ── Users ──────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    github_id = Column(Integer, unique=True, nullable=False)
    github_login = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255), default="")
    avatar_url = Column(String(512), default="")
    created_at = Column(DateTime, default=utc_now)
    last_login_at = Column(DateTime, default=utc_now)

    memberships = relationship("TeamMember", back_populates="user", foreign_keys="[TeamMember.user_id]")
    edge_nodes = relationship("EdgeNode", back_populates="owner")


# ── Teams ──────────────────────────────────────────────────────────────

class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True)
    slug = Column(String(100), unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utc_now)

    members = relationship("TeamMember", back_populates="team")
    boards = relationship("Board", back_populates="team")
    templates = relationship("Template", back_populates="team")


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),)

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String(50), default="member")  # owner | admin | member
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    joined_at = Column(DateTime, default=utc_now)

    team = relationship("Team", back_populates="members")
    user = relationship("User", back_populates="memberships", foreign_keys=[user_id])


# ── Boards ─────────────────────────────────────────────────────────────

class Board(Base):
    __tablename__ = "boards"

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    name = Column(String(255), nullable=False)
    site_url = Column(String(512), default="")
    project_key = Column(String(50), default="")
    jira_board_id = Column(String(50), default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utc_now)
    # Jira credentials (base64 for now — will be encrypted in production)
    jira_email = Column(String(255), default="")
    jira_token = Column(Text, default="")

    team = relationship("Team", back_populates="boards")
    routes = relationship("Route", back_populates="board", cascade="all, delete-orphan")
    sessions = relationship("AgentSession", back_populates="board")
    templates = relationship("Template", back_populates="board")


# ── Routes ─────────────────────────────────────────────────────────────

class Route(Base):
    __tablename__ = "routes"
    __table_args__ = (UniqueConstraint("board_id", "status"),)

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id"), nullable=False)
    status = Column(String(100), nullable=False)
    action = Column(String(100), default="claude_default")
    prompt_template = Column(Text, default="")
    enabled = Column(Boolean, default=False)
    transition_on_launch = Column(String(100), nullable=True)
    transition_on_success = Column(String(100), nullable=True)
    transition_on_failure = Column(String(100), nullable=True)
    allowed_issue_types = Column(Text, default="")  # JSON array stored as text
    created_at = Column(DateTime, default=utc_now)

    board = relationship("Board", back_populates="routes")


# ── Templates ──────────────────────────────────────────────────────────

class Template(Base):
    __tablename__ = "templates"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)  # e.g. "/solve"
    description = Column(Text, default="")
    prompt_template = Column(Text, default="")
    # Scope hierarchy: global (both null) > team (team set) > project (board set)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    board_id = Column(Integer, ForeignKey("boards.id"), nullable=True)
    recommended_transition_on_launch = Column(String(100), nullable=True)
    recommended_transition_on_success = Column(String(100), nullable=True)
    recommended_transition_on_failure = Column(String(100), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    team = relationship("Team", back_populates="templates")
    board = relationship("Board", back_populates="templates")


# ── Edge Nodes ─────────────────────────────────────────────────────────

class EdgeNode(Base):
    __tablename__ = "edge_nodes"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    hostname = Column(String(255), default="")
    os_name = Column(String(100), default="")
    ssh_connect = Column(String(512), default="")
    online = Column(Boolean, default=True)
    last_seen_at = Column(DateTime, default=utc_now)
    registered_at = Column(DateTime, default=utc_now)

    owner = relationship("User", back_populates="edge_nodes")
    sessions = relationship("AgentSession", back_populates="edge_node")


# ── Agent Sessions ─────────────────────────────────────────────────────

class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(255), unique=True, nullable=False)
    board_id = Column(Integer, ForeignKey("boards.id"), nullable=False)
    edge_node_id = Column(Integer, ForeignKey("edge_nodes.id"), nullable=False)
    ticket_key = Column(String(100), nullable=False)
    ticket_summary = Column(Text, default="")
    prompt = Column(Text, default="")
    state = Column(String(50), default="pending")  # pending | launching | running | completed | failed | killed
    result = Column(Text, default="")
    output_snapshot = Column(Text, default="")  # last N lines of terminal output
    launched_at = Column(DateTime, default=utc_now)
    completed_at = Column(DateTime, nullable=True)

    board = relationship("Board", back_populates="sessions")
    edge_node = relationship("EdgeNode", back_populates="sessions")


# ── Heartbeat Assignment ──────────────────────────────────────────────

class HeartbeatAssignment(Base):
    __tablename__ = "heartbeat_assignments"
    __table_args__ = (UniqueConstraint("board_id"),)

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id"), nullable=False)
    primary_edge_id = Column(Integer, ForeignKey("edge_nodes.id"), nullable=False)
    assigned_at = Column(DateTime, default=utc_now)


# ── Helpers ────────────────────────────────────────────────────────────

def create_tables() -> None:
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise

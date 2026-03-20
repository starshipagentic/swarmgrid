"""Team management — create, invite by GitHub username, list members."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .db import SessionLocal, Team, TeamMember, User

router = APIRouter(prefix="/api/teams", tags=["teams"])


class TeamCreate(BaseModel):
    name: str
    slug: str = ""


class InviteRequest(BaseModel):
    github_login: str


def _team_to_dict(t: Team, members: list[dict] | None = None) -> dict:
    d = {"id": t.id, "slug": t.slug, "name": t.name, "created_at": t.created_at.isoformat() if t.created_at else None}
    if members is not None:
        d["members"] = members
    return d


def _member_to_dict(m: TeamMember, user: User) -> dict:
    return {
        "user_id": user.id,
        "github_login": user.github_login,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "role": m.role,
        "joined_at": m.joined_at.isoformat() if m.joined_at else None,
    }


@router.get("")
def list_teams(user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        memberships = db.query(TeamMember).filter(TeamMember.user_id == user.id).all()
        team_ids = [m.team_id for m in memberships]
        teams = db.query(Team).filter(Team.id.in_(team_ids)).all() if team_ids else []
        return {"teams": [_team_to_dict(t) for t in teams]}
    finally:
        db.close()


@router.post("", status_code=201)
def create_team(body: TeamCreate, user: User = Depends(get_current_user)):
    slug = body.slug or re.sub(r"[^a-z0-9-]", "-", body.name.lower()).strip("-")
    db = SessionLocal()
    try:
        if db.query(Team).filter(Team.slug == slug).first():
            raise HTTPException(status_code=409, detail=f"Team slug '{slug}' already taken")
        team = Team(slug=slug, name=body.name, created_by=user.id)
        db.add(team)
        db.flush()
        membership = TeamMember(team_id=team.id, user_id=user.id, role="owner")
        db.add(membership)
        db.commit()
        db.refresh(team)
        return {"ok": True, "team": _team_to_dict(team)}
    finally:
        db.close()


@router.get("/{team_id}")
def get_team(team_id: int, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        team = db.query(Team).filter(Team.id == team_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        membership = db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.user_id == user.id).first()
        if not membership:
            raise HTTPException(status_code=403, detail="Not a member of this team")
        members = db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
        member_dicts = []
        for m in members:
            u = db.query(User).filter(User.id == m.user_id).first()
            if u:
                member_dicts.append(_member_to_dict(m, u))
        return {"team": _team_to_dict(team, members=member_dicts)}
    finally:
        db.close()


@router.post("/{team_id}/invite")
def invite_member(team_id: int, body: InviteRequest, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        membership = db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.user_id == user.id).first()
        if not membership or membership.role not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Only owners/admins can invite members")

        invitee = db.query(User).filter(User.github_login == body.github_login).first()
        if not invitee:
            raise HTTPException(
                status_code=404,
                detail=f"User '{body.github_login}' not found. They must sign in to SwarmGrid first.",
            )

        existing = db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.user_id == invitee.id).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"'{body.github_login}' is already a member")

        new_member = TeamMember(team_id=team_id, user_id=invitee.id, role="member", invited_by=user.id)
        db.add(new_member)
        db.commit()
        return {"ok": True, "invited": body.github_login}
    finally:
        db.close()


@router.get("/{team_id}/members")
def list_members(team_id: int, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        membership = db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.user_id == user.id).first()
        if not membership:
            raise HTTPException(status_code=403, detail="Not a member of this team")
        members = db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
        result = []
        for m in members:
            u = db.query(User).filter(User.id == m.user_id).first()
            if u:
                result.append(_member_to_dict(m, u))
        return {"members": result}
    finally:
        db.close()


@router.delete("/{team_id}/members/{github_login}")
def remove_member(team_id: int, github_login: str, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        membership = db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.user_id == user.id).first()
        if not membership or membership.role not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Only owners/admins can remove members")
        target = db.query(User).filter(User.github_login == github_login).first()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        target_membership = db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.user_id == target.id).first()
        if not target_membership:
            raise HTTPException(status_code=404, detail="User is not a member")
        if target_membership.role == "owner":
            raise HTTPException(status_code=400, detail="Cannot remove the team owner")
        db.delete(target_membership)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

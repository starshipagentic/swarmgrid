"""Template library CRUD — global, team, and project (board) scope."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .db import Board, SessionLocal, Team, TeamMember, Template, User

router = APIRouter(prefix="/api/templates", tags=["templates"])


class TemplateCreate(BaseModel):
    name: str  # e.g. "/solve"
    description: str = ""
    prompt_template: str = ""
    team_id: int | None = None
    board_id: int | None = None
    recommended_transition_on_launch: str | None = None
    recommended_transition_on_success: str | None = None
    recommended_transition_on_failure: str | None = None


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt_template: str | None = None
    recommended_transition_on_launch: str | None = None
    recommended_transition_on_success: str | None = None
    recommended_transition_on_failure: str | None = None


def _template_to_dict(t: Template) -> dict:
    scope = "global"
    if t.board_id:
        scope = "project"
    elif t.team_id:
        scope = "team"
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "prompt_template": t.prompt_template,
        "scope": scope,
        "team_id": t.team_id,
        "board_id": t.board_id,
        "recommended_transition_on_launch": t.recommended_transition_on_launch,
        "recommended_transition_on_success": t.recommended_transition_on_success,
        "recommended_transition_on_failure": t.recommended_transition_on_failure,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("")
def list_templates(
    team_id: int | None = None,
    board_id: int | None = None,
    user: User = Depends(get_current_user),
):
    """List templates visible to the user.

    - No filters: global templates
    - team_id: global + team templates
    - board_id: global + team + project templates (full resolution chain)
    """
    db = SessionLocal()
    try:
        q = db.query(Template)
        if board_id:
            board = db.query(Board).filter(Board.id == board_id).first()
            if not board:
                raise HTTPException(status_code=404, detail="Board not found")
            q = q.filter(
                (Template.team_id.is_(None) & Template.board_id.is_(None))  # global
                | (Template.team_id == board.team_id)  # team
                | (Template.board_id == board_id)  # project
            )
        elif team_id:
            q = q.filter(
                (Template.team_id.is_(None) & Template.board_id.is_(None))  # global
                | (Template.team_id == team_id)  # team
            )
        else:
            q = q.filter(Template.team_id.is_(None), Template.board_id.is_(None))  # global only

        templates = q.order_by(Template.name).all()
        return {"templates": [_template_to_dict(t) for t in templates]}
    finally:
        db.close()


@router.post("", status_code=201)
def create_template(body: TemplateCreate, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        # Verify access
        if body.board_id:
            board = db.query(Board).filter(Board.id == body.board_id).first()
            if not board:
                raise HTTPException(status_code=404, detail="Board not found")
            m = db.query(TeamMember).filter(TeamMember.team_id == board.team_id, TeamMember.user_id == user.id).first()
            if not m:
                raise HTTPException(status_code=403, detail="Not a member of this board's team")
        elif body.team_id:
            m = db.query(TeamMember).filter(TeamMember.team_id == body.team_id, TeamMember.user_id == user.id).first()
            if not m:
                raise HTTPException(status_code=403, detail="Not a member of this team")
        # Global templates — no extra check needed (any authenticated user)

        template = Template(
            name=body.name,
            description=body.description,
            prompt_template=body.prompt_template,
            team_id=body.team_id,
            board_id=body.board_id,
            recommended_transition_on_launch=body.recommended_transition_on_launch,
            recommended_transition_on_success=body.recommended_transition_on_success,
            recommended_transition_on_failure=body.recommended_transition_on_failure,
            created_by=user.id,
        )
        db.add(template)
        db.commit()
        db.refresh(template)
        return {"ok": True, "template": _template_to_dict(template)}
    finally:
        db.close()


@router.get("/{template_id}")
def get_template(template_id: int, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        template = db.query(Template).filter(Template.id == template_id).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        return {"template": _template_to_dict(template)}
    finally:
        db.close()


@router.put("/{template_id}")
def update_template(template_id: int, body: TemplateUpdate, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        template = db.query(Template).filter(Template.id == template_id).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(template, field, value)
        db.commit()
        db.refresh(template)
        return {"ok": True, "template": _template_to_dict(template)}
    finally:
        db.close()


@router.delete("/{template_id}")
def delete_template(template_id: int, user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        template = db.query(Template).filter(Template.id == template_id).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        db.delete(template)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.get("/resolve/{board_id}/{template_name:path}")
def resolve_template(board_id: int, template_name: str, user: User = Depends(get_current_user)):
    """Resolve a template name using the hierarchy: project > team > global."""
    db = SessionLocal()
    try:
        board = db.query(Board).filter(Board.id == board_id).first()
        if not board:
            raise HTTPException(status_code=404, detail="Board not found")

        # 1. Project-level
        t = db.query(Template).filter(Template.board_id == board_id, Template.name == template_name).first()
        if t:
            return {"template": _template_to_dict(t), "resolved_from": "project"}

        # 2. Team-level
        t = db.query(Template).filter(Template.team_id == board.team_id, Template.board_id.is_(None), Template.name == template_name).first()
        if t:
            return {"template": _template_to_dict(t), "resolved_from": "team"}

        # 3. Global
        t = db.query(Template).filter(Template.team_id.is_(None), Template.board_id.is_(None), Template.name == template_name).first()
        if t:
            return {"template": _template_to_dict(t), "resolved_from": "global"}

        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found in any scope")
    finally:
        db.close()

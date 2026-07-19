from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, HTTPException, status
from sqlmodel import Session, select
from app.core.database import get_session
from app.api.deps import get_current_user
from app.core.security import get_password_hash, generate_access_code
from app.models.domain import User, Household
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

router = APIRouter()


@router.post("/api/membros")
async def create_member(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Auto-generate unique access code
    for _ in range(20):
        code = generate_access_code()
        existing = db.exec(select(User).where(User.access_code == code)).first()
        if not existing:
            break
    else:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Não foi possível gerar um código único")

    member = User(
        name=name,
        email=f"{code}@moneyin.local",
        hashed_password=get_password_hash(code),
        household_id=current_user.household_id,
        access_code=code,
    )
    db.add(member)
    db.commit()

    return {"status": "success", "user_id": str(member.id), "access_code": code}


@router.put("/api/membros/{member_id}")
async def update_member(
    member_id: str,
    name: str = Form(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    uid = UUID(member_id)
    member = db.exec(select(User).where(User.id == uid)).first()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membro não encontrado")
    if member.household_id != current_user.household_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado")

    member.name = name
    db.add(member)
    db.commit()

    return {"status": "success"}


@router.delete("/api/membros/{member_id}")
async def delete_member(
    member_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if member_id == str(current_user.id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Não é possível remover a si mesmo")

    uid = UUID(member_id)
    member = db.exec(select(User).where(User.id == uid)).first()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membro não encontrado")
    if member.household_id != current_user.household_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado")

    db.delete(member)
    db.commit()

    return {"status": "success"}

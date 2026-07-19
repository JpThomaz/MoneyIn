from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, HTTPException, status
from sqlmodel import Session, select
from app.core.database import get_session
from app.api.deps import get_current_user
from app.models.domain import User, Category
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

router = APIRouter()


@router.post("/api/categorias")
async def create_category(
    request: Request,
    name: str = Form(...),
    color: str = Form(...),
    type: str = Form(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    category = Category(
        name=name,
        color=color,
        type=type,
        household_id=current_user.household_id,
    )
    db.add(category)
    db.commit()
    db.refresh(category)

    categories = db.exec(
        select(Category).where(
            (Category.household_id == current_user.household_id) | (Category.household_id.is_(None))
        )
    ).all()

    return templates.TemplateResponse(
        request,
        "partials/category_grid.html",
        {"categories": categories},
    )


@router.put("/api/categorias/{category_id}")
async def update_category(
    request: Request,
    category_id: str,
    name: str = Form(...),
    color: str = Form(...),
    type: str = Form(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    uid = UUID(category_id)
    cat = db.exec(select(Category).where(Category.id == uid)).first()
    if not cat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria não encontrada")
    # Only allow editing system categories or own household categories
    if cat.household_id is not None and cat.household_id != current_user.household_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado")

    cat.name = name
    cat.color = color
    cat.type = type
    db.add(cat)
    db.commit()
    db.refresh(cat)

    categories = db.exec(
        select(Category).where(
            (Category.household_id == current_user.household_id) | (Category.household_id.is_(None))
        )
    ).all()

    return templates.TemplateResponse(
        request,
        "partials/category_grid.html",
        {"categories": categories},
    )


@router.delete("/api/categorias/{category_id}")
async def delete_category(
    request: Request,
    category_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    uid = UUID(category_id)
    cat = db.exec(select(Category).where(Category.id == uid)).first()
    if not cat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria não encontrada")
    if cat.household_id is not None and cat.household_id != current_user.household_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado")
    # Allow deleting any category the user can see (including system ones, for their view)
    db.delete(cat)
    db.commit()

    categories = db.exec(
        select(Category).where(
            (Category.household_id == current_user.household_id) | (Category.household_id.is_(None))
        )
    ).all()

    return templates.TemplateResponse(
        request,
        "partials/category_grid.html",
        {"categories": categories},
    )

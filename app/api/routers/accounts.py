from uuid import UUID
from datetime import date

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session, select, func
from app.core.database import get_session
from app.api.deps import get_current_user
from app.models.domain import User, Account, Transaction
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

router = APIRouter()


def _render_accounts_container(request: Request, db: Session, current_user: User):
    accounts = db.exec(
        select(Account).where(Account.household_id == current_user.household_id)
    ).all()
    household_members = db.exec(
        select(User).where(User.household_id == current_user.household_id)
    ).all()
    return templates.TemplateResponse(
        request,
        "partials/accounts_container.html",
        {
            "accounts": accounts,
            "household_members": household_members,
            "current_user_id": current_user.id,
            "future_invoices_by_account_id": {},
        },
    )


@router.get("/api/contas/{account_id}/projection")
async def account_projection(
    account_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    account = db.exec(
        select(Account).where(
            Account.id == UUID(account_id),
            Account.household_id == current_user.household_id,
        )
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")

    today = date.today()
    projection = []
    for m in range(1, 13):
        y = today.year + ((today.month + m - 1) // 12)
        mo = ((today.month + m - 1) % 12) + 1
        total = db.exec(
            select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
                Transaction.account_id == account.id,
                Transaction.household_id == current_user.household_id,
                func.strftime("%m", Transaction.date) == f"{mo:02d}",
                func.strftime("%Y", Transaction.date) == str(y),
            )
        ).one()
        projection.append({
            "month": mo,
            "year": y,
            "total": round(total or 0.0, 2),
        })
    return JSONResponse(content=projection)


@router.post("/api/contas")
async def create_account(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    bank_slug: str = Form(""),
    user_id: str = Form(...),
    balance: float = Form(0.0),
    credit_limit: float | None = Form(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    account = Account(
        name=name,
        type=type,
        bank_slug=bank_slug or None,
        user_id=UUID(user_id),
        household_id=current_user.household_id,
        balance=balance,
        credit_limit=credit_limit if credit_limit and credit_limit > 0 else None,
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    return _render_accounts_container(request, db, current_user)


@router.put("/api/contas/{account_id}")
async def update_account(
    request: Request,
    account_id: str,
    name: str = Form(...),
    type: str = Form(...),
    bank_slug: str = Form(""),
    user_id: str = Form(...),
    balance: float = Form(0.0),
    credit_limit: float | None = Form(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    account = db.exec(
        select(Account).where(
            Account.id == UUID(account_id),
            Account.household_id == current_user.household_id,
        )
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")

    account.name = name
    account.type = type
    account.bank_slug = bank_slug or None
    account.user_id = UUID(user_id)
    account.balance = balance
    account.credit_limit = credit_limit if credit_limit and credit_limit > 0 else None
    db.add(account)
    db.commit()

    return _render_accounts_container(request, db, current_user)


@router.delete("/api/contas/{account_id}")
async def delete_account(
    request: Request,
    account_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    account = db.exec(
        select(Account).where(
            Account.id == UUID(account_id),
            Account.household_id == current_user.household_id,
        )
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")

    db.delete(account)
    db.commit()

    return _render_accounts_container(request, db, current_user)

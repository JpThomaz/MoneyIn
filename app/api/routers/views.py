from datetime import date, datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import HTMLResponse
from sqlmodel import select
from sqlalchemy import func, or_, and_

from app.core.database import get_session
from app.models.domain import Transaction, User, Account, Category
from app.api.deps import get_current_user
from app.api.routers.analytics import (
    compute_kpis,
    compute_cash_flow,
    compute_categories,
    compute_balance_evolution,
    compute_balance_forecast,
    compute_future_commitments,
    compute_member_expenses,
    compute_member_evolution,
)
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")
templates.env.cache_size = 0

router = APIRouter()


def _month_date_range(year: int, month: int):
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _effective_month_filters(household_id: UUID, month: int, year: int, include_projections: bool = False):
    """Filtra transações pelo mês de competência.

    Usa reference_month/year quando disponível (cartão de crédito),
    ou Transaction.date como fallback (conta corrente).
    """
    start, end = _month_date_range(year, month)
    filters = [
        Transaction.household_id == household_id,
        Transaction.is_transfer == False,
        or_(
            and_(
                Transaction.reference_month == month,
                Transaction.reference_year == year,
            ),
            and_(
                Transaction.reference_month.is_(None),
                Transaction.date >= start,
                Transaction.date <= end,
            ),
        ),
    ]
    if not include_projections:
        filters.append(Transaction.status == "CONFIRMED")
    return filters


@router.get("/dashboard")
def dashboard(
    request: Request,
    period: str = Query("this_month"),
    db=Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    hid = current_user.household_id

    kpis = compute_kpis(db, hid, period)
    cash_flow = compute_cash_flow(db, hid, months_count=6)
    categories = compute_categories(db, hid, period)
    balance_evo = compute_balance_evolution(db, hid, months_count=6)
    balance_forecast = compute_balance_forecast(db, hid, historical_months=6, forecast_months=6)
    future_commitments = compute_future_commitments(db, hid)
    member_expenses = compute_member_expenses(db, hid, period)
    member_evolution = compute_member_evolution(db, hid, months_count=6)

    context = {
        "request": request,
        "period": period,
        "kpis": kpis,
        "cash_flow": cash_flow,
        "categories": categories,
        "balance_evo": balance_evo,
        "balance_forecast": balance_forecast,
        "future_commitments": future_commitments,
        "member_expenses": member_expenses,
        "member_evolution": member_evolution,
    }
    template = templates.env.get_template("dashboard.html")
    content = template.render(**context)
    return HTMLResponse(content)


@router.get("/dashboard/partials")
def dashboard_partials(
    request: Request,
    period: str = Query("this_month"),
    db=Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    hid = current_user.household_id

    kpis = compute_kpis(db, hid, period)
    cash_flow = compute_cash_flow(db, hid, months_count=6)
    categories = compute_categories(db, hid, period)
    balance_evo = compute_balance_evolution(db, hid, months_count=6)
    balance_forecast = compute_balance_forecast(db, hid, historical_months=6, forecast_months=6)
    future_commitments = compute_future_commitments(db, hid)
    member_expenses = compute_member_expenses(db, hid, period)
    member_evolution = compute_member_evolution(db, hid, months_count=6)

    context = {
        "request": request,
        "period": period,
        "kpis": kpis,
        "cash_flow": cash_flow,
        "categories": categories,
        "balance_evo": balance_evo,
        "balance_forecast": balance_forecast,
        "future_commitments": future_commitments,
        "member_expenses": member_expenses,
        "member_evolution": member_evolution,
    }
    template = templates.env.get_template("partials/dashboard_content.html")
    content = template.render(**context)
    return HTMLResponse(content)


@router.get("/upload")
def upload_page(request: Request, db=Depends(get_session), current_user: User = Depends(get_current_user)):
    accounts = db.exec(select(Account).where(Account.household_id == current_user.household_id)).all()
    template = templates.env.get_template("upload.html")
    content = template.render(request=request, accounts=accounts)
    return HTMLResponse(content)


@router.get("/categorias")
def categories_page(request: Request, db=Depends(get_session), current_user: User = Depends(get_current_user)):
    household_id = current_user.household_id
    categories = db.exec(
        select(Category).where(
            (Category.household_id == household_id) | (Category.household_id.is_(None))
        )
    ).all()

    today = date.today()
    start, end = _month_date_range(today.year, today.month)

    cat_filters = _effective_month_filters(household_id, today.month, today.year) if household_id else []

    stmt = select(
        Category.name, Category.color,
        func.coalesce(func.sum(func.abs(Transaction.amount)), 0).label("total")
    ).join(
        Transaction, Transaction.category_id == Category.id, isouter=True
    ).where(
        *cat_filters,
        Transaction.amount < 0,
    ).group_by(Category.id, Category.name, Category.color)

    rows = db.exec(stmt).all()
    category_spending = [
        {"name": r.name, "total": float(r.total), "color": r.color}
        for r in rows if r.total > 0
    ]

    template = templates.env.get_template("categories.html")
    content = template.render(
        request=request,
        categories=categories,
        category_spending=category_spending,
        chart_labels=[s["name"] for s in category_spending],
        chart_values=[s["total"] for s in category_spending],
        chart_colors=[s["color"] for s in category_spending],
    )
    return HTMLResponse(content)


@router.get("/membros")
def members_page(
    request: Request,
    month: Optional[int] = Query(None),
    year: Optional[int] = Query(None),
    db=Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    household_id = current_user.household_id
    today = date.today()
    m = month or today.month
    y = year or today.year
    start, end = _month_date_range(y, m)

    members = db.exec(
        select(User).where(User.household_id == household_id)
    ).all()

    member_filters = _effective_month_filters(household_id, m, y)
    member_spending = []
    total_expense = 0.0
    for member in members:
        stmt = select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
            *member_filters,
            Transaction.account.has(user_id=member.id),
            Transaction.amount < 0,
        )
        val = db.exec(stmt).first() or 0.0
        member_spending.append({"name": member.name, "total": float(val)})
        total_expense += float(val)

    avg_expense = total_expense / len(members) if members else 0.0

    template = templates.env.get_template("members.html")
    content = template.render(
        request=request,
        members=members,
        member_spending=member_spending,
        total_expense=total_expense,
        avg_expense=avg_expense,
        current_user_id=current_user.id,
        current_month=m,
        current_year=y,
    )
    return HTMLResponse(content)


@router.get("/contas")
def accounts_page(request: Request, db=Depends(get_session), current_user: User = Depends(get_current_user)):
    accounts = db.exec(
        select(Account).where(Account.household_id == current_user.household_id)
    ).all()

    # Future invoices for credit cards using reference_month/year
    today = date.today()
    future_invoices_by_account_id = {}
    for account in accounts:
        if account.type == "CREDITO":
            invoices = []
            for mo in range(1, 13):
                y = today.year + ((today.month + mo - 1) // 12)
                m = ((today.month + mo - 1) % 12) + 1
                total = db.exec(
                    select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
                        Transaction.account_id == account.id,
                        Transaction.household_id == current_user.household_id,
                        Transaction.reference_month == m,
                        Transaction.reference_year == y,
                    )
                ).one()
                total_val = total or 0.0
                if total_val != 0:
                    invoices.append({
                        "month_ref": f"{m:02d}/{y}",
                        "amount": abs(total_val),
                    })
            future_invoices_by_account_id[str(account.id)] = invoices

    household_members = db.exec(
        select(User).where(User.household_id == current_user.household_id)
    ).all()

    template = templates.env.get_template("accounts.html")
    content = template.render(
        request=request,
        accounts=accounts,
        household_members=household_members,
        current_user_id=current_user.id,
        future_invoices_by_account_id=future_invoices_by_account_id,
    )
    return HTMLResponse(content)

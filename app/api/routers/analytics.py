import json
import asyncio
from datetime import date, timedelta
from typing import Optional, List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse
from sqlmodel import Session, select
from sqlalchemy import func, or_, and_

from app.core.database import get_session
from app.core.config import settings
from app.models.domain import Transaction, Category, Account, AccountBalanceHistory, User
from app.api.deps import get_current_user

router = APIRouter()

# ---------------------------------------------------------------------------
# Transaction-based monthly cash flow helper (fallback for empty balance history)
# ---------------------------------------------------------------------------

def _compute_monthly_cash_flows(
    db: Session,
    household_id: UUID,
    start_date: date,
    end_date: date,
) -> Dict[str, float]:
    """Compute net cash flow per month from CONFIRMED transactions.

    Returns dict like {"2026-01": -450.30, "2026-02": 1200.00, ...}.
    Amounts: positive = income, negative = expense.
    """
    stmt = (
        select(
            Transaction.date,
            Transaction.amount,
            Transaction.reference_month,
            Transaction.reference_year,
        )
        .where(
            Transaction.household_id == household_id,
            Transaction.status == "CONFIRMED",
            Transaction.is_transfer == False,
            Transaction.date >= start_date,
            Transaction.date <= end_date,
        )
        .order_by(Transaction.date)
    )
    rows = db.exec(stmt).all()

    monthly: Dict[str, float] = {}
    for tx_date, amount, ref_month, ref_year in rows:
        # Use reference_month/year if set (credit card), else transaction date
        if ref_month and ref_year:
            ym = f"{ref_year:04d}-{ref_month:02d}"
        else:
            ym = tx_date.strftime("%Y-%m")
        monthly[ym] = round(monthly.get(ym, 0.0) + float(amount or 0.0), 2)

    return monthly


def _compute_cumulative_cash_flow(
    db: Session,
    household_id: UUID,
    month_keys: List[str],
) -> List[float]:
    """Compute cumulative net cash flow for each month in month_keys.

    Returns list of cumulative balances starting from 0.
    e.g. [0, -450, 750, ...] meaning: Jan=0, Feb=-450, Mar=750
    """
    if not month_keys:
        return []

    # Get date range from month_keys
    first_key = month_keys[0]  # "2026-01"
    last_key = month_keys[-1]  # "2026-06"
    start = date(int(first_key[:4]), int(first_key[5:7]), 1)
    # End = last day of last month
    y, m = int(last_key[:4]), int(last_key[5:7])
    if m == 12:
        end = date(y, 12, 31)
    else:
        end = date(y, m + 1, 1) - timedelta(days=1)

    flows = _compute_monthly_cash_flows(db, household_id, start, end)

    result: List[float] = []
    cumulative = 0.0
    for k in month_keys:
        cumulative += flows.get(k, 0.0)
        result.append(round(cumulative, 2))

    return result


# ---------------------------------------------------------------------------
# Period resolution helpers
# ---------------------------------------------------------------------------

# Map period keys to (month, year) or date ranges
_PERIOD_THIS_MONTH = "this_month"
_PERIOD_LAST_3_MONTHS = "last_3_months"
_PERIOD_LAST_6_MONTHS = "last_6_months"
_PERIOD_LAST_12_MONTHS = "last_12_months"


def _month_date_range(year: int, month: int):
    """Retorna (start, end) de um mês específico."""
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _resolve_period(period: str, ref_date: date = None):
    """Resolve string de período em lista de (year, month).

    Retorna:
      months: list of (year, month) tuples
      label: human-readable label for the period
    """
    ref = ref_date or date.today()
    if period == _PERIOD_LAST_3_MONTHS:
        months = []
        for i in range(3):
            m = ((ref.month - i - 1) % 12) + 1
            y = ref.year + ((ref.month - i - 1) // 12)
            months.append((y, m))
        return months, "Últimos 3 meses"
    elif period == _PERIOD_LAST_6_MONTHS:
        months = []
        for i in range(6):
            m = ((ref.month - i - 1) % 12) + 1
            y = ref.year + ((ref.month - i - 1) // 12)
            months.append((y, m))
        return months, "Últimos 6 meses"
    elif period == _PERIOD_LAST_12_MONTHS:
        months = []
        for i in range(12):
            m = ((ref.month - i - 1) % 12) + 1
            y = ref.year + ((ref.month - i - 1) // 12)
            months.append((y, m))
        return months, "Últimos 12 meses"
    else:  # this_month or default
        return [(ref.year, ref.month)], "Este mês"


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


# ---------------------------------------------------------------------------
# KPI Card aggregations
# ---------------------------------------------------------------------------

def compute_kpis(db: Session, household_id: UUID, period: str = _PERIOD_THIS_MONTH):
    """Calcula os KPIs do dashboard: Receita, Despesa, Saldo, Ritmo Semanal.

    Retorna dict com:
      income, expense, balance, period_label,
      weekly_current, weekly_previous, weekly_change_pct
    """
    today = date.today()
    months, period_label = _resolve_period(period, today)

    total_income = 0.0
    total_expense = 0.0

    for y, m in months:
        filters = _effective_month_filters(household_id, m, y)
        inc = db.exec(
            select(func.coalesce(func.sum(Transaction.amount), 0))
            .where(*filters, Transaction.amount > 0)
        ).first() or 0.0
        exp = db.exec(
            select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0))
            .where(*filters, Transaction.amount < 0)
        ).first() or 0.0
        total_income += float(inc)
        total_expense += float(exp)

    # Balanço semanal: últimos 7 dias vs 7 dias anteriores
    week_end = today
    week_start = today - timedelta(days=6)
    prev_end = today - timedelta(days=7)
    prev_start = today - timedelta(days=13)

    weekly_current = float(
        db.exec(
            select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
                Transaction.household_id == household_id,
                Transaction.is_transfer == False,
                Transaction.amount < 0,
                Transaction.date >= week_start,
                Transaction.date <= week_end,
            )
        ).first() or 0.0
    )

    weekly_previous = float(
        db.exec(
            select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
                Transaction.household_id == household_id,
                Transaction.is_transfer == False,
                Transaction.amount < 0,
                Transaction.date >= prev_start,
                Transaction.date <= prev_end,
            )
        ).first() or 0.0
    )

    weekly_change_pct = 0.0
    if weekly_previous > 0:
        weekly_change_pct = ((weekly_current - weekly_previous) / weekly_previous) * 100

    return {
        "income": round(total_income, 2),
        "expense": round(total_expense, 2),
        "balance": round(total_income - total_expense, 2),
        "period_label": period_label,
        "period": period,
        "weekly_current": round(weekly_current, 2),
        "weekly_previous": round(weekly_previous, 2),
        "weekly_change_pct": round(weekly_change_pct, 1),
    }


# ---------------------------------------------------------------------------
# Chart 1: Cash Flow (Fluxo de Caixa) — monthly income vs expense
# ---------------------------------------------------------------------------

def compute_cash_flow(db: Session, household_id: UUID, months_count: int = 6):
    """Agrupa entradas e saídas por mês para o gráfico de Fluxo de Caixa.

    Retorna:
      labels: list of "Jan 2026" strings
      income_data: list of positive amounts per month
      expense_data: list of absolute negative amounts per month
    """
    today = date.today()
    labels = []
    income_data = []
    expense_data = []

    for i in range(months_count - 1, -1, -1):
        m = ((today.month - i - 1) % 12) + 1
        y = today.year + ((today.month - i - 1) // 12)
        start, end = _month_date_range(y, m)
        labels.append(start.strftime("%b %Y"))

        filters = [
            Transaction.household_id == household_id,
            Transaction.is_transfer == False,
            Transaction.status == "CONFIRMED",
            or_(
                and_(Transaction.reference_month == m, Transaction.reference_year == y),
                and_(Transaction.reference_month.is_(None), Transaction.date >= start, Transaction.date <= end),
            ),
        ]

        inc = float(
            db.exec(
                select(func.coalesce(func.sum(Transaction.amount), 0))
                .where(*filters, Transaction.amount > 0)
            ).first() or 0.0
        )
        exp = float(
            db.exec(
                select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0))
                .where(*filters, Transaction.amount < 0)
            ).first() or 0.0
        )

        income_data.append(round(inc, 2))
        expense_data.append(round(exp, 2))

    return {
        "labels": labels,
        "income_data": income_data,
        "expense_data": expense_data,
    }


# ---------------------------------------------------------------------------
# Chart 2: Categories (Despesas por Categoria) — donut chart
# ---------------------------------------------------------------------------

def compute_categories(db: Session, household_id: UUID, period: str = _PERIOD_THIS_MONTH):
    """Agrupa despesas por categoria no período selecionado.

    Retorna:
      names: list of category names
      values: list of summed absolute amounts
      colors: list of category colors
      total: total expense for center label
    """
    today = date.today()
    months, _ = _resolve_period(period, today)

    cat_sums: Dict[str, float] = {}
    cat_colors: Dict[str, str] = {}

    for y, m in months:
        filters = _effective_month_filters(household_id, m, y)
        rows = db.exec(
            select(
                func.coalesce(Category.name, Transaction.description).label("cat_name"),
                func.coalesce(Category.color, "#6b7280").label("cat_color"),
                func.coalesce(func.sum(func.abs(Transaction.amount)), 0).label("total"),
            )
            .select_from(Transaction)
            .join(Category, Transaction.category_id == Category.id, isouter=True)
            .where(*filters, Transaction.amount < 0)
            .group_by("cat_name", "cat_color")
        ).all()

        for row in rows:
            name = str(row[0] or "Outros").upper()
            color = str(row[1] or "#6b7280")
            val = float(row[2] or 0.0)
            cat_sums[name] = cat_sums.get(name, 0.0) + val
            cat_colors[name] = color

    # Sort by value descending
    sorted_cats = sorted(cat_sums.items(), key=lambda x: x[1], reverse=True)

    names = [c[0] for c in sorted_cats]
    amounts = [round(c[1], 2) for c in sorted_cats]
    colors = [cat_colors.get(c[0], "#6b7280") for c in sorted_cats]
    total = round(sum(amounts), 2)

    return {
        "names": names,
        "amounts": amounts,
        "colors": colors,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Chart 3: Balance Evolution (Evolução de Saldo)
# ---------------------------------------------------------------------------

def compute_balance_evolution(db: Session, household_id: UUID, months_count: int = 6):
    """Busca histórico de saldo somado de todas as contas, agregado por mês.

    Fallback: se AccountBalanceHistory estiver vazio, calcula fluxo de caixa
    acumulado a partir de transações CONFIRMED.

    Retorna:
      labels: list of "MM/YYYY" strings (oldest first)
      amounts: list of summed balances
    """
    today = date.today()
    # Build target month keys: YYYY-MM strings (oldest first)
    target_keys = []
    target_labels = []
    for i in range(months_count, 0, -1):
        d = today.replace(day=1)
        m = d.month - i
        y = d.year
        while m <= 0:
            m += 12
            y -= 1
        target_keys.append(f"{y:04d}-{m:02d}")
        target_labels.append(f"{m:02d}/{y}")

    # Query AccountBalanceHistory
    start_date = date(int(target_keys[0][:4]), int(target_keys[0][5:7]), 1)
    stmt = (
        select(
            func.strftime("%Y-%m", AccountBalanceHistory.updated_at).label("ym"),
            func.sum(AccountBalanceHistory.balance).label("total_balance"),
        )
        .join(Account, AccountBalanceHistory.account_id == Account.id)
        .where(
            Account.household_id == household_id,
            AccountBalanceHistory.updated_at >= start_date,
        )
        .group_by("ym")
        .order_by("ym")
    )
    rows = db.exec(stmt).all()
    db_map = {str(ym): round(float(bal or 0.0), 2) for ym, bal in rows}

    amounts = [db_map.get(k) for k in target_keys]

    # Fallback 1: if no history, try cumulative cash flow from transactions
    if all(v is None for v in amounts):
        amounts = _compute_cumulative_cash_flow(db, household_id, target_keys)

    # Fallback 2: if still no data, use current account balances
    if all(v == 0.0 for v in amounts) and amounts:
        accounts = db.exec(
            select(Account).where(Account.household_id == household_id)
        ).all()
        total_balance = sum(float(a.balance or 0.0) for a in accounts)
        amounts = [round(total_balance, 2)] * len(amounts)

    # Fill any remaining None gaps with nearest known
    clean: list = []
    last_known = 0.0
    for v in amounts:
        if v is not None:
            last_known = v
        clean.append(round(last_known, 2))

    return {
        "labels": target_labels,
        "amounts": clean,
    }


# ---------------------------------------------------------------------------
# Chart 3b: Balance Forecast (Previsão de Saldo)
# ---------------------------------------------------------------------------

def _month_keys_and_labels(
    today: date,
    past_months: int,
    future_months: int,
) -> tuple[List[str], List[str]]:
    """Return (db_keys, display_labels) for past+future months.

    db_keys:    ["2026-01", "2026-02", ... "2026-12"] — for DB strftime lookup
    display:    ["01/2026", "02/2026", ... "12/2026"] — for chart axis labels
    """
    db_keys: List[str] = []
    labels: List[str] = []
    total = past_months + future_months
    for i in range(total):
        offset = i - past_months  # negative = past, 0 = current, positive = future
        m = today.month + offset
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        db_keys.append(f"{y:04d}-{m:02d}")
        labels.append(f"{m:02d}/{y}")
    return db_keys, labels


def compute_balance_forecast(
    db: Session,
    household_id: UUID,
    historical_months: int = 6,
    forecast_months: int = 6,
):
    """Build historical monthly balance totals and forecast future months.

    Returns dict where ALL arrays have identical length = historical_months + forecast_months.

    Keys:
      labels:            list of "MM/YYYY" strings
      amounts:           historical balances (None for forecast months)
      forecast_values:   forecast balances (None for historical months)
      lower_bound:       lower CI (None for historical)
      upper_bound:       upper CI (None for historical)
      method:            forecasting method used
    """
    from app.services.forecasting_service import forecast_balance

    today = date.today()
    total = historical_months + forecast_months

    # Build month keys and labels
    db_keys, all_labels = _month_keys_and_labels(today, historical_months, forecast_months)

    # Query DB for historical months only
    hist_start_key = db_keys[0]  # e.g. "2026-01"
    stmt = (
        select(
            func.strftime("%Y-%m", AccountBalanceHistory.updated_at).label("ym"),
            func.sum(AccountBalanceHistory.balance).label("total_balance"),
        )
        .join(Account, AccountBalanceHistory.account_id == Account.id)
        .where(
            Account.household_id == household_id,
            AccountBalanceHistory.updated_at >= f"{hist_start_key}-01",
        )
        .group_by("ym")
        .order_by("ym")
    )
    rows = db.exec(stmt).all()
    db_map = {str(ym): round(float(bal or 0.0), 2) for ym, bal in rows}

    # Map historical keys to values
    hist_values = [db_map.get(k) for k in db_keys[:historical_months]]

    # Fallback 1: if no history, compute cumulative cash flow from transactions
    if all(v is None for v in hist_values):
        hist_values = _compute_cumulative_cash_flow(
            db, household_id, db_keys[:historical_months]
        )

    # Fallback 2: if still empty, use current account balances
    if all(v == 0.0 for v in hist_values) and hist_values:
        accounts = db.exec(
            select(Account).where(Account.household_id == household_id)
        ).all()
        total_balance = sum(float(a.balance or 0.0) for a in accounts)
        hist_values = [round(total_balance, 2)] * historical_months

    # Fill any remaining None gaps with nearest known
    clean_values: List[float] = []
    last_known = 0.0
    for v in hist_values:
        if v is not None:
            last_known = v
        clean_values.append(round(last_known, 2))

    # --- Run forecast engine ---
    history_for_engine = [{"balance": b} for b in clean_values]
    result = forecast_balance(history_for_engine, periods=forecast_months)

    if result:
        fc_vals = result.forecast_values
        lower = result.lower_bound
        upper = result.upper_bound
        method = result.method
    else:
        last = clean_values[-1] if clean_values else 0.0
        margin = abs(last) * 0.10
        fc_vals = [round(last, 2)] * forecast_months
        lower = [round(last - margin, 2)] * forecast_months
        upper = [round(last + margin, 2)] * forecast_months
        method = "fallback"

    # --- Build output arrays — ALL guaranteed same length = total ---
    all_amounts = clean_values + [None] * forecast_months
    all_forecast: list = [None] * historical_months + fc_vals
    all_lower: list = [None] * historical_months + lower
    all_upper: list = [None] * historical_months + upper

    assert len(all_labels) == total, f"labels({len(all_labels)}) != {total}"
    assert len(all_amounts) == total, f"amounts({len(all_amounts)}) != {total}"
    assert len(all_forecast) == total, f"forecast({len(all_forecast)}) != {total}"
    assert len(all_lower) == total, f"lower({len(all_lower)}) != {total}"
    assert len(all_upper) == total, f"upper({len(all_upper)}) != {total}"

    return {
        "labels": all_labels,
        "amounts": all_amounts,
        "forecast_values": all_forecast,
        "lower_bound": all_lower,
        "upper_bound": all_upper,
        "method": method,
    }


# ---------------------------------------------------------------------------
# Chart 4: Future Commitments (Comprometimento Futuro)
# ---------------------------------------------------------------------------

def compute_future_commitments(db: Session, household_id: UUID):
    """Agrupa transações PROJECTED pelos meses futuros.

    Retorna:
      labels: list of "Mês/Ano" strings
      values: list of absolute amounts
    """
    today = date.today()
    labels = []
    amounts = []

    for i in range(1, 13):
        m = ((today.month + i - 1) % 12) + 1
        y = today.year + ((today.month + i - 1) // 12)
        month_label = f"{m:02d}/{y}"

        total = float(
            db.exec(
                select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
                    Transaction.household_id == household_id,
                    Transaction.is_transfer == False,
                    Transaction.status == "PROJECTED",
                    Transaction.reference_month == m,
                    Transaction.reference_year == y,
                )
            ).first() or 0.0
        )

        if total > 0:
            labels.append(month_label)
            amounts.append(round(total, 2))

    return {
        "labels": labels,
        "amounts": amounts,
    }


# ---------------------------------------------------------------------------
# Chart 5: Member Expenses — Distribuição de gastos por membro no mês
# ---------------------------------------------------------------------------

# Fixed palette — must stay consistent across all charts
MEMBER_COLORS = ["#6366f1", "#14b8a6", "#f59e0b", "#f43f5e", "#8b5cf6", "#06b6d4", "#84cc16", "#ec4899"]


def compute_member_expenses(db: Session, household_id: UUID, period: str = _PERIOD_THIS_MONTH):
    """Agrupa despesas por membro (via conta vinculada) para o período.

    Retorna:
      names: list of member names
      amounts: list of absolute expense amounts (NaN-safe, always float)
      colors: list of hex colors from MEMBER_COLORS
      total: total expense for center label
      member_color_map: dict mapping member_id -> color hex
    """
    today = date.today()
    months, _ = _resolve_period(period, today)

    members = db.exec(
        select(User.id, User.name).where(User.household_id == household_id)
    ).all()
    member_map = {m.id: m.name for m in members}
    member_color_map = {m.id: MEMBER_COLORS[i % len(MEMBER_COLORS)] for i, m in enumerate(members)}

    member_sums: Dict[str, float] = {str(mid): 0.0 for mid in member_map}

    for y, m in months:
        filters = _effective_month_filters(household_id, m, y)
        rows = db.exec(
            select(
                Account.user_id,
                func.coalesce(func.sum(func.abs(Transaction.amount)), 0).label("total"),
            )
            .select_from(Transaction)
            .join(Account, Transaction.account_id == Account.id, isouter=True)
            .where(*filters, Transaction.amount < 0)
            .group_by(Account.user_id)
        ).all()

        for row in rows:
            uid = str(row[0]) if row[0] else None
            if uid and uid in member_sums:
                member_sums[uid] += float(row[1] or 0.0)

    sorted_members = sorted(member_sums.items(), key=lambda x: x[1], reverse=True)
    names = [member_map.get(UUID(k), "Desconhecido") for k, v in sorted_members if v > 0]
    amounts = [round(v, 2) for _, v in sorted_members if v > 0]
    colors = [member_color_map.get(UUID(k), "#6b7280") for k, v in sorted_members if v > 0]
    total = round(sum(amounts), 2)

    return {
        "names": names,
        "amounts": amounts,
        "colors": colors,
        "total": total,
        "member_color_map": {str(k): v for k, v in member_color_map.items()},
    }


# ---------------------------------------------------------------------------
# Chart 6: Member Evolution — Despesas mensais por membro (últimos 6 meses)
# ---------------------------------------------------------------------------

def compute_member_evolution(db: Session, household_id: UUID, months_count: int = 6):
    """Despesas mensais dos últimos N meses, quebradas por membro.

    Retorna:
      labels: list of "Mês Ano" strings (oldest → newest)
      members: list of dicts { name, amounts: [float], color }
      member_ids: ordered list of member UUIDs (to map colors in frontend)
    """
    today = date.today()

    members = db.exec(
        select(User.id, User.name).where(User.household_id == household_id)
    ).all()
    member_order = [(m.id, m.name) for m in members]
    member_color_map = {m.id: MEMBER_COLORS[i % len(MEMBER_COLORS)] for i, m in enumerate(members)}

    month_keys = []
    month_labels = []
    for i in range(months_count - 1, -1, -1):
        m = ((today.month - i - 1) % 12) + 1
        y = today.year + ((today.month - i - 1) // 12)
        month_keys.append((y, m))
        start = date(y, m, 1)
        month_labels.append(start.strftime("%b %Y"))

    raw: Dict[str, Dict[str, float]] = {}
    for mid, mname in member_order:
        raw[str(mid)] = {}
        for y, m in month_keys:
            raw[str(mid)][f"{y}-{m:02d}"] = 0.0

    for y, m in month_keys:
        filters = _effective_month_filters(household_id, m, y)
        rows = db.exec(
            select(
                Account.user_id,
                func.coalesce(func.sum(func.abs(Transaction.amount)), 0).label("total"),
            )
            .select_from(Transaction)
            .join(Account, Transaction.account_id == Account.id, isouter=True)
            .where(*filters, Transaction.amount < 0)
            .group_by(Account.user_id)
        ).all()

        for row in rows:
            uid = str(row[0]) if row[0] else None
            if uid and uid in raw:
                raw[uid][f"{y}-{m:02d}"] = float(row[1] or 0.0)

    members_data = []
    for mid, mname in member_order:
        mid_str = str(mid)
        member_amounts = [round(raw[mid_str].get(f"{y}-{m:02d}", 0.0), 2) for y, m in month_keys]
        members_data.append({
            "name": mname,
            "amounts": member_amounts,
            "color": member_color_map.get(mid, "#6b7280"),
        })

    return {
        "labels": month_labels,
        "members": members_data,
        "member_ids": [str(mid) for mid, _ in member_order],
    }


# ---------------------------------------------------------------------------
# JSON API endpoints (for potential AJAX usage)
# ---------------------------------------------------------------------------

@router.get("/analytics/summary")
def analytics_summary(
    period: str = _PERIOD_THIS_MONTH,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_kpis(db, current_user.household_id, period)


@router.get("/analytics/expenses-by-category")
def expenses_by_category(
    period: str = _PERIOD_THIS_MONTH,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_categories(db, current_user.household_id, period)


@router.get("/analytics/cash-flow")
def cash_flow(
    months: int = 6,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_cash_flow(db, current_user.household_id, months)


@router.get("/analytics/balance-evolution")
def balance_evolution(
    months: int = 6,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_balance_evolution(db, current_user.household_id, months)


@router.get("/analytics/future-commitments")
def future_commitments(
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_future_commitments(db, current_user.household_id)


@router.get("/analytics/member-expenses")
def member_expenses(
    period: str = _PERIOD_THIS_MONTH,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_member_expenses(db, current_user.household_id, period)


@router.get("/analytics/member-evolution")
def member_evolution(
    months: int = 6,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_member_evolution(db, current_user.household_id, months)


@router.get("/analytics/balance-forecast")
def balance_forecast(
    historical_months: int = 6,
    forecast_months: int = 6,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return compute_balance_forecast(
        db, current_user.household_id,
        historical_months=historical_months,
        forecast_months=forecast_months,
    )


# ---------------------------------------------------------------------------
# Synthetic data seeding — for testing forecasting without real history
# ---------------------------------------------------------------------------

@router.post("/analytics/seed-balance-history")
def seed_balance_history(
    months: int = 12,
    base_balance: float = 5000.0,
    volatility: float = 0.08,
    trend: float = 150.0,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Generate synthetic AccountBalanceHistory records for the household's accounts.

    This is a dev/test endpoint to populate realistic balance history when
    the database has insufficient records for forecasting.
    """
    import random
    from datetime import datetime

    hid = current_user.household_id
    accounts = db.exec(select(Account).where(Account.household_id == hid)).all()
    if not accounts:
        return {"error": "No accounts found for this household"}

    # Use first account as primary
    account = accounts[0]
    today = date.today()

    inserted = 0
    for i in range(months):
        m = ((today.month - months + i) % 12) + 1
        y = today.year + ((today.month - months + i - 1) // 12)
        if y < today.year - 1 or (y == today.year - 1 and m > today.month):
            continue

        noise = random.gauss(0, abs(base_balance) * volatility)
        balance = base_balance + (trend * i) + noise
        ts = datetime(y, m, 15, 12, 0, 0)

        record = AccountBalanceHistory(
            account_id=account.id,
            balance=round(balance, 2),
            updated_at=ts,
        )
        db.add(record)
        inserted += 1

    db.commit()
    return {"ok": True, "records_created": inserted, "account_id": str(account.id)}


# ---------------------------------------------------------------------------
# AI Insights — Proactive financial insights via Gemini (with fallback)
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory="app/templates")


@router.get("/analytics/insights")
def get_insights(
    request: Request,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """AI-powered insights endpoint. Returns rendered HTML partial for HTMX lazy loading."""
    from app.services.ai_assistant import get_insights as get_ai_insights

    insights = get_ai_insights(db, current_user.household_id)
    content = templates.env.get_template("partials/insights_cards.html").render(insights=insights)
    return HTMLResponse(content)

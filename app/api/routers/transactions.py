from datetime import date, datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select, Session
from sqlalchemy import or_, and_

from app.core.database import get_session
from app.api.deps import get_current_user
from app.models.domain import (
    Transaction, User, Account, Category, FileImport,
)
from app.services.crypto_service import generate_transaction_hash

SORT_COLUMNS = {
    "date": Transaction.date,
    "description": Transaction.description,
    "amount": Transaction.amount,
    "installment": Transaction.installment_number,
}

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


def _query_transactions(
    db, household_id, month, year, start, end,
    show_transfers, hide_projections,
    account_id=None, user_id=None, category_id=None,
    search=None, sort_by="date", sort_dir="desc",
):
    conditions = [
        Transaction.household_id == household_id,
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
    if not show_transfers:
        conditions.append(Transaction.is_transfer == False)
    if hide_projections:
        conditions.append(Transaction.status == "CONFIRMED")
    if account_id:
        conditions.append(Transaction.account_id == UUID(account_id))
    if user_id:
        conditions.append(Transaction.user_id == UUID(user_id))
    if category_id:
        conditions.append(Transaction.category_id == UUID(category_id))
    if search and search.strip():
        like = f"%{search.strip()}%"
        conditions.append(Transaction.description.ilike(like))

    sort_col = SORT_COLUMNS.get(sort_by, Transaction.date)
    order = sort_col.desc() if sort_dir == "desc" else sort_col.asc()
    stmt = select(Transaction).where(*conditions).order_by(order, Transaction.date.desc())
    return db.exec(stmt).all()


# ─── PÁGINA PRINCIPAL ─────────────────────────────────────────────

@router.get("/extrato", response_class=HTMLResponse)
async def statement_page(
    request: Request,
    month: Optional[int] = Query(None),
    year: Optional[int] = Query(None),
    account_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    show_transfers: Optional[bool] = Query(False),
    hide_projections: Optional[bool] = Query(True),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("date"),
    sort_dir: Optional[str] = Query("desc"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    household_id = current_user.household_id
    today = date.today()
    m = month or today.month
    y = year or today.year
    start, end = _month_date_range(y, m)

    transactions = _query_transactions(
        db, household_id, m, y, start, end,
        show_transfers, hide_projections,
        account_id, user_id, category_id,
        search, sort_by, sort_dir,
    )

    accounts = db.exec(
        select(Account).where(Account.household_id == household_id).order_by(Account.name)
    ).all()
    categories = db.exec(
        select(Category).where(
            (Category.household_id == household_id) | (Category.household_id.is_(None))
        ).order_by(Category.name)
    ).all()
    members = db.exec(
        select(User).where(User.household_id == household_id).order_by(User.name)
    ).all()
    file_imports = db.exec(
        select(FileImport).where(FileImport.household_id == household_id).order_by(FileImport.uploaded_at.desc())
    ).all()

    total_income = sum(t.amount for t in transactions if t.amount > 0 and not t.is_transfer)
    total_expense = sum(abs(t.amount) for t in transactions if t.amount < 0 and not t.is_transfer)

    accounts_map = {a.id: a for a in accounts}
    categories_map = {c.id: c for c in categories}
    members_map = {m.id: m for m in members}

    template = templates.env.get_template("statement.html")
    content = template.render(
        request=request,
        transactions=transactions,
        accounts=accounts,
        categories=categories,
        members=members,
        accounts_map=accounts_map,
        categories_map=categories_map,
        members_map=members_map,
        file_imports=file_imports,
        current_month=m,
        current_year=y,
        selected_account=account_id or "",
        selected_user=user_id or "",
        selected_category=category_id or "",
        show_transfers=show_transfers,
        hide_projections=hide_projections,
        search=search or "",
        sort_by=sort_by or "date",
        sort_dir=sort_dir or "desc",
        total_income=total_income,
        total_expense=total_expense,
        balance=total_income - total_expense,
        current_user_id=current_user.id,
    )
    return HTMLResponse(content)


# ─── PARCIAL: TABELA DE TRANSAÇÕES (para filtros via HTMX) ─────

@router.get("/api/transactions", response_class=HTMLResponse)
async def list_transactions_partial(
    request: Request,
    month: Optional[int] = Query(None),
    year: Optional[int] = Query(None),
    account_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    show_transfers: Optional[bool] = Query(False),
    hide_projections: Optional[bool] = Query(True),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("date"),
    sort_dir: Optional[str] = Query("desc"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    household_id = current_user.household_id
    today = date.today()
    m = month or today.month
    y = year or today.year
    start, end = _month_date_range(y, m)

    transactions = _query_transactions(
        db, household_id, m, y, start, end,
        show_transfers, hide_projections,
        account_id, user_id, category_id,
        search, sort_by, sort_dir,
    )

    total_income = sum(t.amount for t in transactions if t.amount > 0 and not t.is_transfer)
    total_expense = sum(abs(t.amount) for t in transactions if t.amount < 0 and not t.is_transfer)
    balance = total_income - total_expense

    accounts_map = {a.id: a for a in db.exec(select(Account)).all()}
    categories_map = {c.id: c for c in db.exec(select(Category)).all()}
    members_map = {m.id: m for m in db.exec(select(User)).all()}

    template = templates.env.get_template("partials/statement_content.html")
    content = template.render(
        request=request,
        transactions=transactions,
        accounts_map=accounts_map,
        categories_map=categories_map,
        members_map=members_map,
        current_user_id=current_user.id,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        hide_projections=hide_projections,
        search=search or "",
        sort_by=sort_by or "date",
        sort_dir=sort_dir or "desc",
    )
    return HTMLResponse(content)


# ─── FORMULÁRIO DE NOVA TRANSAÇÃO (parcial) ─────────────────────

@router.get("/api/transactions/new-form", response_class=HTMLResponse)
async def new_transaction_form(
    request: Request,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    accounts = db.exec(
        select(Account).where(Account.household_id == current_user.household_id).order_by(Account.name)
    ).all()
    categories = db.exec(
        select(Category).where(
            (Category.household_id == current_user.household_id) | (Category.household_id.is_(None))
        ).order_by(Category.name)
    ).all()
    members = db.exec(
        select(User).where(User.household_id == current_user.household_id).order_by(User.name)
    ).all()

    template = templates.env.get_template("partials/transaction_new_form.html")
    content = template.render(
        request=request,
        accounts=accounts,
        categories=categories,
        members=members,
        today=date.today(),
    )
    return HTMLResponse(content)


# ─── EXIBIR UMA LINHA (view mode) ──────────────────────────────

@router.get("/api/transactions/{tx_id}", response_class=HTMLResponse)
async def get_transaction_row(
    request: Request,
    tx_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return await _render_single_row(request, tx_id, db, current_user)


# ─── CRIAR TRANSAÇÃO MANUAL ──────────────────────────────────────

@router.post("/api/transactions/manual", response_class=HTMLResponse)
async def create_manual_transaction(
    request: Request,
    date: str = Form(...),
    description: str = Form(...),
    amount: float = Form(...),
    account_id: str = Form(...),
    category_id: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    try:
        tx_date = date.fromisoformat(date)
    except Exception:
        tx_date = datetime.now().date()

    tx_hash = generate_transaction_hash(date, amount, description, account_id)

    existing = db.exec(
        select(Transaction).where(
            Transaction.transaction_hash == tx_hash,
            Transaction.household_id == current_user.household_id,
        )
    ).first()
    if existing:
        return await _render_single_row(request, existing.id, db, current_user)

    new_tx = Transaction(
        date=tx_date,
        description=description.strip(),
        amount=amount,
        account_id=UUID(account_id),
        category_id=UUID(category_id) if category_id else None,
        user_id=UUID(user_id) if user_id else None,
        household_id=current_user.household_id,
        is_transfer=False,
        transaction_hash=tx_hash,
        file_import_id=None,
    )
    db.add(new_tx)
    db.commit()
    db.refresh(new_tx)

    return await _render_single_row(request, new_tx.id, db, current_user)


# ─── EDITAR TRANSAÇÃO (GET parcial — formulário inline) ─────────

@router.get("/api/transactions/{tx_id}/edit", response_class=HTMLResponse)
async def edit_transaction_form(
    request: Request,
    tx_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    tx = db.exec(
        select(Transaction).where(
            Transaction.id == UUID(tx_id),
            Transaction.household_id == current_user.household_id,
        )
    ).first()
    if not tx:
        return HTMLResponse("")

    accounts = db.exec(
        select(Account).where(Account.household_id == current_user.household_id).order_by(Account.name)
    ).all()
    categories = db.exec(
        select(Category).where(
            (Category.household_id == current_user.household_id) | (Category.household_id.is_(None))
        ).order_by(Category.name)
    ).all()
    members = db.exec(
        select(User).where(User.household_id == current_user.household_id).order_by(User.name)
    ).all()

    template = templates.env.get_template("partials/transaction_row_edit.html")
    content = template.render(
        request=request,
        tx=tx,
        accounts=accounts,
        categories=categories,
        members=members,
        current_user_id=current_user.id,
    )
    return HTMLResponse(content)


# ─── ATUALIZAR TRANSAÇÃO ─────────────────────────────────────────

@router.put("/api/transactions/{tx_id}", response_class=HTMLResponse)
async def update_transaction(
    request: Request,
    tx_id: str,
    date: str = Form(...),
    description: str = Form(...),
    amount: float = Form(...),
    account_id: str = Form(...),
    category_id: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    tx = db.exec(
        select(Transaction).where(
            Transaction.id == UUID(tx_id),
            Transaction.household_id == current_user.household_id,
        )
    ).first()
    if not tx:
        return HTMLResponse("")

    try:
        tx.date = date.fromisoformat(date)
    except Exception:
        pass
    tx.description = description.strip()
    tx.amount = amount
    tx.account_id = UUID(account_id)
    tx.category_id = UUID(category_id) if category_id else None
    tx.user_id = UUID(user_id) if user_id else None

    # Recalculate hash in case data changed
    new_hash = generate_transaction_hash(date, amount, description, account_id)
    existing = db.exec(
        select(Transaction).where(
            Transaction.transaction_hash == new_hash,
            Transaction.household_id == current_user.household_id,
            Transaction.id != tx.id,
        )
    ).first()
    if not existing:
        tx.transaction_hash = new_hash

    db.add(tx)
    db.commit()
    db.refresh(tx)

    return await _render_single_row(request, tx.id, db, current_user)


# ─── EXCLUIR TRANSAÇÃO ──────────────────────────────────────────

@router.delete("/api/transactions/{tx_id}", response_class=HTMLResponse)
async def delete_transaction(
    request: Request,
    tx_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    tx = db.exec(
        select(Transaction).where(
            Transaction.id == UUID(tx_id),
            Transaction.household_id == current_user.household_id,
        )
    ).first()
    if tx:
        db.delete(tx)
        db.commit()
    return HTMLResponse("")


# ─── EXCLUSÃO EM LOTE ───────────────────────────────────────────

@router.delete("/api/transactions/batch/{file_import_id}", response_class=HTMLResponse)
async def batch_delete_transactions(
    request: Request,
    file_import_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    fi = db.exec(
        select(FileImport).where(
            FileImport.id == UUID(file_import_id),
            FileImport.household_id == current_user.household_id,
        )
    ).first()
    if fi:
        # Delete all transactions linked to this import
        txs = db.exec(
            select(Transaction).where(Transaction.file_import_id == fi.id)
        ).all()
        for tx in txs:
            db.delete(tx)
        db.delete(fi)
        db.commit()

    return HTMLResponse(
        content="""
        <div id="batch-delete-toast"
             class="fixed top-4 right-4 z-50 rounded-lg border border-emerald-200 bg-emerald-50 px-5 py-3 text-sm font-medium text-emerald-800 shadow-lg transition-all">
            Lote excluído com sucesso.
        </div>
        <script>setTimeout(function(){ var el = document.getElementById('batch-delete-toast'); if(el) el.remove(); }, 3000);</script>
        """
    )


# ─── HELPERS ─────────────────────────────────────────────────────

async def _render_single_row(request: Request, tx_id, db, current_user):
    tx = db.exec(select(Transaction).where(Transaction.id == tx_id)).first()
    if not tx:
        return HTMLResponse("")
    accounts_map = {a.id: a for a in db.exec(select(Account)).all()}
    categories_map = {c.id: c for c in db.exec(select(Category)).all()}
    members_map = {m.id: m for m in db.exec(select(User)).all()}
    template = templates.env.get_template("partials/transaction_row.html")
    content = template.render(
        request=request,
        tx=tx,
        accounts_map=accounts_map,
        categories_map=categories_map,
        members_map=members_map,
        current_user_id=current_user.id,
    )
    return HTMLResponse(content)

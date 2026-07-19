from pathlib import Path
import os

from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect
from sqlmodel import SQLModel

from app.core.config import settings
from app.core.database import engine
from app.api.routers.upload import router as upload_router
from app.api.routers.analytics import router as analytics_router
from app.api.routers.setup import router as setup_router
from app.api.routers.auth import router as auth_router
from app.api.routers.views import router as views_router
from app.api.routers.categories import router as categories_router
from app.api.routers.accounts import router as accounts_router
from app.api.routers.members import router as members_router
from app.api.routers.transactions import router as transactions_router
from app.api.routers.chat import router as chat_router
from app.models.domain import Category

app = FastAPI(title="Money In API")

# Jinja2 templates directory for server-rendered pages
templates = Jinja2Templates(directory="app/templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api", tags=["auth"])
app.include_router(upload_router, prefix="/api", tags=["upload"])
app.include_router(analytics_router, prefix="/api", tags=["analytics"])
app.include_router(setup_router, prefix="/api", tags=["setup"])
app.include_router(categories_router, tags=["categories"])
app.include_router(accounts_router, tags=["accounts"])
app.include_router(members_router, tags=["members"])
app.include_router(transactions_router, tags=["transactions"])
app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(views_router, tags=["views"])  # server-rendered pages


def _ensure_sqlite_schema() -> None:
    if not settings.DATABASE_URL or not settings.DATABASE_URL.startswith("sqlite"):
        return

    database_path = engine.url.database
    if not database_path:
        return

    db_file = Path(database_path)
    if not db_file.exists():
        return

    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    if not existing_tables:
        return

    with engine.connect() as conn:
        for table, column, sql in [
            ("user", "profile_picture_url",
             "ALTER TABLE user ADD COLUMN profile_picture_url VARCHAR"),
            ("category", "color",
             "ALTER TABLE category ADD COLUMN color VARCHAR NOT NULL DEFAULT '#6b7280'"),
            ("account", "bank_slug",
             "ALTER TABLE account ADD COLUMN bank_slug VARCHAR"),
            ("account", "balance",
             "ALTER TABLE account ADD COLUMN balance FLOAT DEFAULT 0.0"),
            ("account", "credit_limit",
             "ALTER TABLE account ADD COLUMN credit_limit FLOAT"),
            ("transaction", "user_id",
             "ALTER TABLE transaction ADD COLUMN user_id VARCHAR"),
            ("transaction", "file_import_id",
             "ALTER TABLE transaction ADD COLUMN file_import_id VARCHAR"),
            ("transaction", "installment_number",
             "ALTER TABLE transaction ADD COLUMN installment_number INTEGER"),
            ("transaction", "total_installments",
             "ALTER TABLE transaction ADD COLUMN total_installments INTEGER"),
            ("transaction", "reference_month",
             "ALTER TABLE transaction ADD COLUMN reference_month INTEGER"),
            ("transaction", "reference_year",
             "ALTER TABLE transaction ADD COLUMN reference_year INTEGER"),
            ("transaction", "status",
             "ALTER TABLE transaction ADD COLUMN status VARCHAR DEFAULT 'CONFIRMED'"),
            ("fileimport", "status",
             "ALTER TABLE fileimport ADD COLUMN status VARCHAR DEFAULT 'PENDING'"),
            ("fileimport", "progress_message",
             "ALTER TABLE fileimport ADD COLUMN progress_message VARCHAR"),
            ("fileimport", "error_message",
             "ALTER TABLE fileimport ADD COLUMN error_message VARCHAR"),
            ("fileimport", "payload",
             "ALTER TABLE fileimport ADD COLUMN payload TEXT"),
            ("user", "access_code",
             "ALTER TABLE user ADD COLUMN access_code VARCHAR"),
        ]:
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column in cols:
                continue
            conn.exec_driver_sql(sql)
            print(f"  ✓ Added column {table}.{column}")

        # Migrate transaction_hash to UNIQUE index (reversing previous migration)
        if "transaction" in existing_tables:
            indexes = inspector.get_indexes("transaction")
            existing_unique = None
            existing_nonunique = None
            for idx_info in indexes:
                cols = idx_info.get("columns", [])
                if "transaction_hash" not in cols:
                    continue
                if idx_info.get("unique", False):
                    existing_unique = idx_info["name"]
                else:
                    existing_nonunique = idx_info["name"]

            if existing_unique:
                print(f"  ✓ transaction_hash already UNIQUE ({existing_unique})")
            else:
                if existing_nonunique:
                    conn.exec_driver_sql(f"DROP INDEX {existing_nonunique}")
                    print(f"  ✓ Dropped non-unique index {existing_nonunique}")
                conn.exec_driver_sql("CREATE UNIQUE INDEX ix_transaction_transaction_hash ON transaction(transaction_hash)")
                print("  ✓ Created UNIQUE index on transaction_hash")

        conn.commit()


def _seed_default_categories() -> None:
    from sqlmodel import Session as SqlSession, select
    default_categories = [
        ("Salário", "income", "#10b981"),
        ("Freelance / Autônomo", "income", "#059669"),
        ("Investimentos", "income", "#047857"),
        ("Aluguel", "expense", "#ef4444"),
        ("Condomínio", "expense", "#dc2626"),
        ("Água", "expense", "#3b82f6"),
        ("Luz", "expense", "#f59e0b"),
        ("Internet", "expense", "#6366f1"),
        ("Telefone", "expense", "#8b5cf6"),
        ("Supermercado", "expense", "#f97316"),
        ("Farmácia", "expense", "#ec4899"),
        ("Transporte", "expense", "#14b8a6"),
        ("Combustível", "expense", "#06b6d4"),
        ("Educação", "expense", "#84cc16"),
        ("Saúde", "expense", "#22c55e"),
        ("Lazer", "expense", "#a855f7"),
        ("Restaurante", "expense", "#e11d48"),
        ("Vestuário", "expense", "#be123c"),
        ("Assinaturas", "expense", "#64748b"),
        ("Seguros", "expense", "#78716c"),
        ("Impostos", "expense", "#d97706"),
        ("Outros", "expense", "#6b7280"),
        ("Outros", "income", "#6b7280"),
    ]
    with SqlSession(engine) as db:
        existing = db.exec(select(Category).where(Category.household_id.is_(None))).all()
        existing_names = {(c.name.lower(), c.type) for c in existing}
        new_count = 0
        for name, typ, color in default_categories:
            if (name.lower(), typ) not in existing_names:
                db.add(Category(name=name, type=typ, color=color, household_id=None))
                new_count += 1
        if new_count:
            db.commit()
            print(f"  ✓ Seeded {new_count} default categories")


def _fix_projected_transactions() -> None:
    """Migra transações PROJECTED com reference_month/year no passado para CONFIRMED.

    Isso corrige o bug onde parcelas futuras geradas por faturas antigas
    ficaram como PROJECTED mesmo depois de seus meses terem passado.
    """
    from datetime import date as date_type
    from sqlmodel import Session as SqlSession, select
    from app.models.domain import Transaction

    today = date_type.today()
    current_month_start = date_type(today.year, today.month, 1)
    with SqlSession(engine) as db:
        projected = db.exec(
            select(Transaction).where(
                Transaction.status == "PROJECTED",
                Transaction.reference_month.isnot(None),
                Transaction.reference_year.isnot(None),
            )
        ).all()
        fixed = 0
        for tx in projected:
            ref_date = date_type(tx.reference_year, tx.reference_month, 1)
            if ref_date < current_month_start:
                tx.status = "CONFIRMED"
                tx.date = ref_date
                db.add(tx)
                fixed += 1
        if fixed:
            db.commit()
            print(f"  ✓ Fixed {fixed} PROJECTED → CONFIRMED (past months)")


def _recover_orphaned_imports() -> None:
    """Re-queue PENDING/PROCESSING imports that were interrupted by a server restart."""
    from sqlmodel import Session as SqlSession, select
    from app.models.domain import FileImport
    from app.services.transaction_service import FILE_IMPORT_STATUS_PENDING, FILE_IMPORT_STATUS_PROCESSING

    with SqlSession(engine) as db:
        orphaned = db.exec(
            select(FileImport).where(
                FileImport.status.in_([FILE_IMPORT_STATUS_PENDING, FILE_IMPORT_STATUS_PROCESSING])
            )
        ).all()

        if not orphaned:
            return

        print(f"  Found {len(orphaned)} orphaned import(s), resetting to PENDING...")

        from app.services.transaction_service import FILE_IMPORT_STATUS_PENDING as PENDING
        for fi in orphaned:
            fi.status = PENDING
            fi.progress_message = "Reagendado após reinicialização do servidor"
            fi.error_message = None
            db.add(fi)
        db.commit()

        import asyncio
        from app.api.routers.upload import _process_file_import_background

        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        for fi in orphaned:
            if loop and loop.is_running():
                asyncio.ensure_future(_process_file_import_background(fi.id, fi.household_id))
            else:
                print(f"  ⚠ Cannot re-queue {fi.display_name} — no running event loop")


@app.on_event("startup")
def on_startup() -> None:
    print("  Running create_all...")
    SQLModel.metadata.create_all(engine)
    print("  Running schema migration...")
    _ensure_sqlite_schema()
    print("  Seeding default categories...")
    _seed_default_categories()
    print("  Fixing projected transactions...")
    _fix_projected_transactions()
    print("  Recovering orphaned imports...")
    _recover_orphaned_imports()
    print("  Startup complete.")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Home page with access code login form."""
    return """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Money In - Login</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-br from-slate-900 to-slate-800 min-h-screen flex items-center justify-center">
        <div class="w-full max-w-md">
            <div class="bg-slate-800 rounded-lg shadow-xl p-8">
                <h1 class="text-3xl font-bold text-center mb-2 text-emerald-400">Money In</h1>
                <p class="text-center text-slate-400 mb-8">Gestão Financeira Familiar</p>

                <div id="code-login-section">
                    <p class="text-sm text-slate-300 text-center mb-4">Digite seu código de 6 dígitos</p>
                    <div id="code-inputs" class="flex justify-center gap-2 mb-6">
                        <input type="text" maxlength="1" inputmode="numeric" pattern="[0-9]"
                            class="code-digit w-12 h-14 text-center text-2xl font-bold bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-500/30"
                            data-index="0" />
                        <input type="text" maxlength="1" inputmode="numeric" pattern="[0-9]"
                            class="code-digit w-12 h-14 text-center text-2xl font-bold bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-500/30"
                            data-index="1" />
                        <input type="text" maxlength="1" inputmode="numeric" pattern="[0-9]"
                            class="code-digit w-12 h-14 text-center text-2xl font-bold bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-500/30"
                            data-index="2" />
                        <input type="text" maxlength="1" inputmode="numeric" pattern="[0-9]"
                            class="code-digit w-12 h-14 text-center text-2xl font-bold bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-500/30"
                            data-index="3" />
                        <input type="text" maxlength="1" inputmode="numeric" pattern="[0-9]"
                            class="code-digit w-12 h-14 text-center text-2xl font-bold bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-500/30"
                            data-index="4" />
                        <input type="text" maxlength="1" inputmode="numeric" pattern="[0-9]"
                            class="code-digit w-12 h-14 text-center text-2xl font-bold bg-slate-700 border border-slate-600 rounded-lg text-white focus:outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-500/30"
                            data-index="5" />
                    </div>

                    <button id="loginCodeBtn" type="button"
                        class="w-full bg-emerald-500 hover:bg-emerald-600 text-white font-semibold py-2.5 rounded-lg transition disabled:opacity-40 disabled:cursor-not-allowed"
                        disabled>
                        Entrar
                    </button>

                    <div class="mt-6 text-center border-t border-slate-700 pt-5">
                        <p class="text-sm text-slate-400">
                            Admin?
                            <a href="#" onclick="showAdminLogin()" class="text-emerald-400 hover:text-emerald-300 font-medium transition-colors">Entrar com email e senha</a>
                        </p>
                    </div>
                </div>

                <div id="admin-login-section" class="hidden">
                    <form id="loginForm" class="space-y-4">
                        <div>
                            <label class="block text-sm font-medium text-slate-200 mb-2">Email</label>
                            <input type="email" name="username" required
                                class="w-full px-4 py-2 bg-slate-700 border border-slate-600 rounded-lg text-white placeholder-slate-400 focus:outline-none focus:border-emerald-400"
                                placeholder="seu@email.com" />
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-slate-200 mb-2">Senha</label>
                            <input type="password" name="password" required
                                class="w-full px-4 py-2 bg-slate-700 border border-slate-600 rounded-lg text-white placeholder-slate-400 focus:outline-none focus:border-emerald-400"
                                placeholder="••••••••" />
                        </div>
                        <button type="submit"
                            class="w-full bg-emerald-500 hover:bg-emerald-600 text-white font-semibold py-2 rounded-lg transition">
                            Entrar
                        </button>
                    </form>
                    <div class="mt-4 text-center">
                        <a href="#" onclick="showCodeLogin()" class="text-sm text-slate-400 hover:text-slate-300 transition-colors">← Voltar ao login por código</a>
                    </div>
                    <div class="mt-4 text-center border-t border-slate-700 pt-4">
                        <p class="text-sm text-slate-400">
                            Ainda não tem uma conta?
                            <a href="#" onclick="showRegister()" class="text-emerald-400 hover:text-emerald-300 font-medium transition-colors">Criar Conta</a>
                        </p>
                    </div>
                </div>

                <div id="register-form" class="hidden mt-6 border-t border-slate-700 pt-6">
                    <h2 class="text-lg font-semibold text-white mb-4 text-center">Criar Conta</h2>
                    <form id="registerForm" class="space-y-4">
                        <div>
                            <label class="block text-sm font-medium text-slate-200 mb-2">Nome</label>
                            <input type="text" name="name" required
                                class="w-full px-4 py-2 bg-slate-700 border border-slate-600 rounded-lg text-white placeholder-slate-400 focus:outline-none focus:border-emerald-400"
                                placeholder="Seu nome completo" />
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-slate-200 mb-2">Email</label>
                            <input type="email" name="email" required
                                class="w-full px-4 py-2 bg-slate-700 border border-slate-600 rounded-lg text-white placeholder-slate-400 focus:outline-none focus:border-emerald-400"
                                placeholder="seu@email.com" />
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-slate-200 mb-2">Senha</label>
                            <input type="password" name="password" required
                                class="w-full px-4 py-2 bg-slate-700 border border-slate-600 rounded-lg text-white placeholder-slate-400 focus:outline-none focus:border-emerald-400"
                                placeholder="••••••••" />
                        </div>
                        <button type="submit"
                            class="w-full bg-emerald-500 hover:bg-emerald-600 text-white font-semibold py-2 rounded-lg transition">
                            Criar Conta
                        </button>
                    </form>
                    <div id="register-message" class="mt-4 p-3 rounded-lg hidden"></div>
                    <p class="mt-4 text-center">
                        <a href="#" onclick="showAdminLogin()" class="text-sm text-slate-400 hover:text-slate-300 transition-colors">Já tem conta? Faça login</a>
                    </p>
                </div>

                <div id="message" class="mt-4 p-3 rounded-lg hidden"></div>
            </div>
        </div>

        <script>
            function showCodeLogin() {
                document.getElementById('code-login-section').classList.remove('hidden');
                document.getElementById('admin-login-section').classList.add('hidden');
                document.getElementById('register-form').classList.add('hidden');
                document.getElementById('message').classList.add('hidden');
            }
            function showAdminLogin() {
                document.getElementById('code-login-section').classList.add('hidden');
                document.getElementById('admin-login-section').classList.remove('hidden');
                document.getElementById('register-form').classList.add('hidden');
                document.getElementById('message').classList.add('hidden');
            }
            function showRegister() {
                document.getElementById('register-form').classList.remove('hidden');
                document.getElementById('admin-login-section').classList.add('hidden');
                document.getElementById('code-login-section').classList.add('hidden');
                document.getElementById('message').classList.add('hidden');
            }

            // Access code input logic
            const codeDigits = document.querySelectorAll('.code-digit');
            const loginCodeBtn = document.getElementById('loginCodeBtn');

            codeDigits.forEach((input, idx) => {
                input.addEventListener('input', (e) => {
                    const val = e.target.value.replace(/[^0-9]/g, '');
                    e.target.value = val;
                    if (val && idx < codeDigits.length - 1) {
                        codeDigits[idx + 1].focus();
                    }
                    updateLoginBtn();
                });
                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Backspace' && !e.target.value && idx > 0) {
                        codeDigits[idx - 1].focus();
                    }
                    if (e.key === 'Enter') {
                        attemptCodeLogin();
                    }
                });
                input.addEventListener('paste', (e) => {
                    e.preventDefault();
                    const paste = (e.clipboardData || window.clipboardData).getData('text').replace(/[^0-9]/g, '');
                    for (let i = 0; i < Math.min(paste.length, 6); i++) {
                        codeDigits[i].value = paste[i];
                    }
                    codeDigits[Math.min(paste.length, 5)].focus();
                    updateLoginBtn();
                });
            });

            function updateLoginBtn() {
                const code = getCode();
                loginCodeBtn.disabled = code.length !== 6;
            }

            function getCode() {
                return Array.from(codeDigits).map(d => d.value).join('');
            }

            loginCodeBtn.addEventListener('click', attemptCodeLogin);

            async function attemptCodeLogin() {
                const code = getCode();
                if (code.length !== 6) return;
                const msgDiv = document.getElementById('message');
                loginCodeBtn.disabled = true;
                loginCodeBtn.textContent = 'Entrando...';

                try {
                    const res = await fetch('/api/auth/login-code', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ code: code })
                    });
                    const data = await res.json();
                    if (res.ok) {
                        localStorage.setItem('token', data.access_token);
                        window.location.href = '/dashboard';
                    } else {
                        msgDiv.classList.remove('hidden');
                        msgDiv.className = 'mt-4 p-3 rounded-lg bg-red-900 border border-red-700 text-red-200 text-sm';
                        msgDiv.textContent = data.detail || 'Código inválido';
                        codeDigits.forEach(d => d.value = '');
                        codeDigits[0].focus();
                    }
                } catch (err) {
                    msgDiv.classList.remove('hidden');
                    msgDiv.className = 'mt-4 p-3 rounded-lg bg-red-900 border border-red-700 text-red-200 text-sm';
                    msgDiv.textContent = 'Erro de conexão';
                } finally {
                    loginCodeBtn.disabled = false;
                    loginCodeBtn.textContent = 'Entrar';
                    updateLoginBtn();
                }
            }

            // Admin email/password login
            document.getElementById('loginForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const formData = new FormData(e.target);
                const msgDiv = document.getElementById('message');
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams(formData)
                });
                const data = await res.json();
                if (res.ok) {
                    localStorage.setItem('token', data.access_token);
                    window.location.href = '/dashboard';
                } else {
                    msgDiv.classList.remove('hidden');
                    msgDiv.className = 'mt-4 p-3 rounded-lg bg-red-900 border border-red-700 text-red-200 text-sm';
                    msgDiv.textContent = data.detail || 'Erro ao fazer login';
                }
            });

            // Register
            document.getElementById('registerForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const formData = new FormData(e.target);
                const msgDiv = document.getElementById('register-message');
                const res = await fetch('/api/auth/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: formData.get('name'),
                        email: formData.get('email'),
                        password: formData.get('password'),
                        invite_code: null
                    })
                });
                const data = await res.json();
                if (res.ok) {
                    msgDiv.classList.remove('hidden', 'bg-red-900', 'border-red-700', 'text-red-200');
                    msgDiv.classList.add('bg-emerald-900', 'border', 'border-emerald-700', 'text-emerald-200');
                    msgDiv.textContent = 'Conta criada! Faça login com seu email e senha.';
                    setTimeout(() => showAdminLogin(), 2000);
                } else {
                    msgDiv.classList.remove('hidden');
                    msgDiv.classList.add('bg-red-900', 'border', 'border-red-700', 'text-red-200');
                    msgDiv.textContent = data.detail || 'Erro ao criar conta';
                }
            });
        </script>
    </body>
    </html>
    """

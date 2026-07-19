import json
from pathlib import Path
import tempfile
from uuid import uuid4

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Request, BackgroundTasks
from sqlmodel import Session, select
from sqlalchemy import or_
from uuid import UUID

from app.services import file_service, ai_service
from app.services.file_service import PDFEncryptedException
from app.core.database import engine, get_session
from app.api.deps import get_current_user
from app.models.domain import User, Category, Transaction, Account, AccountBalanceHistory, FileImport
from app.services.crypto_service import generate_transaction_hash
from app.services.transaction_service import (
    FILE_IMPORT_STATUS_PENDING,
    FILE_IMPORT_STATUS_EXTRACTING,
    FILE_IMPORT_STATUS_PROCESSING,
    FILE_IMPORT_STATUS_DONE,
    FILE_IMPORT_STATUS_FAILED,
    create_file_import,
    save_invoice_transactions,
    process_invoice_payments,
    process_file_import_payload,
    _build_transaction_date,
    _is_credit_card_refund,
    _doc_type_label,
)
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from datetime import date, datetime
import fitz

PENDING_DIR = Path(tempfile.gettempdir()) / "pending_uploads"
PENDING_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory="app/templates")
templates.env.cache_size = 0

router = APIRouter()

BANK_SLUG_MAP = {
    "nubank": "nubank",
    "itau": "itau",
    "itaú": "itau",
    "bradesco": "bradesco",
    "sicredi": "sicredi",
    "santander": "santander",
    "caixa": "caixa",
    "banco do brasil": "bb",
    "bb": "bb",
    "inter": "inter",
    "banco inter": "inter",
    "outro": "outro",
}

DOC_TYPE_ACCOUNT_TYPE = {
    "EXTRATO": "CORRENTE",
    "FATURA": "CREDITO",
}


def _normalize_bank_slug(bank_name: str | None) -> str | None:
    if not bank_name:
        return None
    key = bank_name.strip().lower()
    key = key.replace("banco ", "").strip()
    return BANK_SLUG_MAP.get(key, key)


def _match_account(
    db: Session,
    household_id: UUID,
    bank_slug: str | None,
    last_4: str | None,
    doc_type: str | None,
) -> Account | None:
    if not bank_slug:
        return None
    expected_type = DOC_TYPE_ACCOUNT_TYPE.get(doc_type) if doc_type else None

    # 1) Try bank_slug + last_4_digits
    if bank_slug and last_4 and len(last_4) == 4:
        stmt = select(Account).where(
            Account.household_id == household_id,
            Account.bank_slug == bank_slug,
            Account.last_4_digits == last_4,
        )
        acct = db.exec(stmt).first()
        if acct:
            return acct

    # 2) Try bank_slug + account type
    if bank_slug and expected_type:
        stmt = select(Account).where(
            Account.household_id == household_id,
            Account.bank_slug == bank_slug,
            Account.type == expected_type,
        )
        if last_4 and len(last_4) == 4:
            stmt = stmt.where(
                or_(Account.last_4_digits == last_4, Account.last_4_digits.is_(None))
            )
        acct = db.exec(stmt).first()
        if acct:
            return acct

    # 3) Fallback: any account with that bank_slug
    if bank_slug:
        stmt = select(Account).where(
            Account.household_id == household_id,
            Account.bank_slug == bank_slug,
        )
        if last_4 and len(last_4) == 4:
            stmt = stmt.where(
                or_(Account.last_4_digits == last_4, Account.last_4_digits.is_(None))
            )
        acct = db.exec(stmt).first()
        if acct:
            return acct

    return None


def _validate_file_content(file_bytes: bytes, filename: str) -> dict:
    """Valida formato e conteúdo do arquivo sem processamento completo.
    
    Returns dict with: status, filename, has_text?, text_length?, file_id?, error?
    """
    if not filename or "." not in filename:
        return {"status": "error", "error": "Formato não suportado"}
    ext = filename.rsplit(".", 1)[-1].lower()
    
    if ext == "pdf":
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            if doc.is_encrypted:
                file_id = str(uuid4())
                temp_path = PENDING_DIR / file_id
                temp_path.write_bytes(file_bytes)
                doc.close()
                return {"status": "encrypted", "filename": filename, "file_id": file_id}
            text_len = 0
            for page in doc:
                text_len += len(page.get_text("text"))
            doc.close()
            return {"status": "ok", "filename": filename, "has_text": text_len > 20, "text_length": text_len}
        except Exception:
            return {"status": "error", "error": "PDF inválido ou corrompido"}
    
    if ext in {"csv", "txt"}:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = file_bytes.decode("latin-1")
            except Exception:
                return {"status": "error", "error": "Codificação não suportada"}
        return {"status": "ok", "filename": filename, "has_text": len(text.strip()) > 10, "text_length": len(text)}
    
    return {"status": "error", "error": "Formato não suportado. Use PDF, CSV ou TXT"}


@router.post("/upload-statement/validate-file")
async def validate_upload_file(
    file: UploadFile = File(...),
):
    """Valida arquivo antes do upload completo. Verifica formato,
    senha e conteúdo mínimo. Retorna JSON para feedback imediato."""
    file_bytes = await file.read()
    return _validate_file_content(file_bytes, file.filename or "arquivo")


@router.post("/upload-statement/verify-password")
async def verify_password(
    file_id: str = Form(...),
    password: str = Form(...),
):
    """Verifica se a senha de um PDF criptografado está correta.

    O arquivo deve ter sido salvo em PENDING_DIR pelo validate-file.
    Retorna JSON: {status, filename} ou {status, error, file_id, error_message}.
    """
    temp_path = PENDING_DIR / file_id
    if not temp_path.exists():
        return JSONResponse(
            {"status": "error", "error": "Arquivo expirado ou não encontrado. Faça o upload novamente."},
            status_code=404,
        )
    try:
        doc = fitz.open(stream=temp_path.read_bytes(), filetype="pdf")
    except Exception:
        return JSONResponse(
            {"status": "error", "error": "Arquivo corrompido."},
            status_code=400,
        )
    try:
        if doc.is_encrypted:
            ok = doc.authenticate(password)
            if not ok:
                return JSONResponse(
                    {"status": "error", "error": "Senha inválida."},
                    status_code=400,
                )
        return {"status": "ok", "filename": temp_path.name}
    finally:
        doc.close()


@router.post("/upload-statement")
async def upload_statement(
    background_tasks: BackgroundTasks,
    request: Request,
    files: list[UploadFile] = File(...),
    passwords: str = Form("[]"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Máximo de 5 arquivos por vez")

    password_list: list[str | None] = []
    try:
        parsed = json.loads(passwords)
        password_list = [p if p else None for p in (parsed if isinstance(parsed, list) else [])]
    except (json.JSONDecodeError, TypeError):
        password_list = []

    household_id = current_user.household_id

    categories = db.exec(
        select(Category).where(
            (Category.household_id == household_id) | (Category.household_id.is_(None))
        )
    ).all()
    category_names = [c.name for c in categories]

    household_members = db.exec(
        select(User).where(User.household_id == household_id)
    ).all()

    household_accounts = db.exec(
        select(Account).where(Account.household_id == household_id)
    ).all()

    file_results = []
    extraction_imports = []

    for idx, file in enumerate(files):
        file_bytes = await file.read()
        pwd = password_list[idx] if idx < len(password_list) else None

        extraction_fi = create_file_import(
            db,
            filename=file.filename or "arquivo",
            bank_name=None,
            household_id=household_id,
            doc_type=None,
            payload=None,
        )
        extraction_fi.status = FILE_IMPORT_STATUS_EXTRACTING
        extraction_fi.progress_message = "Extraindo dados do arquivo"
        db.add(extraction_fi)
        db.commit()
        db.refresh(extraction_fi)
        extraction_imports.append(extraction_fi)

        try:
            text = await file_service.extract_text_from_file(file_bytes, file.filename, password=pwd)
        except PDFEncryptedException:
            file_id = uuid4()
            temp_path = PENDING_DIR / str(file_id)
            temp_path.write_bytes(file_bytes)
            extraction_fi.status = FILE_IMPORT_STATUS_FAILED
            extraction_fi.progress_message = "PDF protegido — senha necessária"
            db.add(extraction_fi)
            db.commit()
            file_results.append({
                "type": "password_needed",
                "file_id": str(file_id),
                "filename": file.filename,
                "error": None,
            })
            continue
        except HTTPException:
            extraction_fi.status = FILE_IMPORT_STATUS_FAILED
            extraction_fi.progress_message = "Formato não suportado"
            db.add(extraction_fi)
            db.commit()
            file_results.append({
                "filename": file.filename,
                "error": "Formato não suportado ou arquivo corrompido",
            })
            continue

        extraction_fi.progress_message = "Analisando com IA"
        db.add(extraction_fi)
        db.commit()

        try:
            analysis = await ai_service.analyze_financial_text(text, category_names)
        except Exception as e:
            print(f"AI error for {file.filename}: {e}")
            extraction_fi.status = FILE_IMPORT_STATUS_FAILED
            extraction_fi.progress_message = "Falha na análise de IA"
            db.add(extraction_fi)
            db.commit()
            file_results.append({
                "filename": file.filename,
                "error": "Falha na análise de IA",
            })
            continue

        if not analysis.documento_valido:
            extraction_fi.status = FILE_IMPORT_STATUS_FAILED
            extraction_fi.progress_message = analysis.codigo_erro or "Documento inválido"
            db.add(extraction_fi)
            db.commit()
            file_results.append({
                "filename": file.filename,
                "error": analysis.codigo_erro or "Documento inválido",
            })
            continue

        doc_type = analysis.tipo_documento or None
        bank_name = analysis.banco_identificado
        bank_slug = _normalize_bank_slug(bank_name)
        last_4 = analysis.quatro_ultimos_digitos
        saldo_final = analysis.saldo_final_extrato
        transactions = [t.model_dump() for t in analysis.transacoes]

        if doc_type == "FATURA":
            for tx in transactions:
                tx["data"] = _build_transaction_date(
                    tx,
                    invoice_month=analysis.mes_vencimento,
                    invoice_year=analysis.ano_vencimento,
                )
                desc = str(tx.get("description_clean") or tx.get("descricao", "")).strip()
                is_refund = _is_credit_card_refund(desc)
                if not is_refund:
                    try:
                        val = float(tx.get("valor", 0))
                        if val > 0:
                            tx["valor"] = -val
                    except (ValueError, TypeError):
                        pass

        account = _match_account(db, household_id, bank_slug, last_4, doc_type)
        account_exists = account is not None

        date_part = datetime.utcnow().strftime("%d/%m/%Y")
        parts = [p for p in [bank_name, _doc_type_label(doc_type), date_part] if p]
        display_name = " - ".join(parts) if parts else file.filename
        extraction_fi.display_name = display_name
        extraction_fi.status = FILE_IMPORT_STATUS_DONE
        extraction_fi.progress_message = f"Extraído — {len(transactions)} transação{'ões' if len(transactions) != 1 else ''}"
        db.add(extraction_fi)
        db.commit()

        file_results.append({
            "filename": file.filename,
            "extraction_import_id": str(extraction_fi.id),
            "doc_type": doc_type,
            "bank_name": bank_name,
            "bank_slug": bank_slug,
            "last_4": last_4,
            "saldo_final": saldo_final,
            "mes_vencimento": analysis.mes_vencimento,
            "ano_vencimento": analysis.ano_vencimento,
            "account_id": str(account.id) if account else None,
            "account_name": account.name if account else None,
            "account_exists": account_exists,
            "transactions": transactions,
            "error": None,
        })

    return templates.TemplateResponse(
        request,
        "partials/multi_review_carousel.html",
        {
            "file_results": file_results,
            "categories": categories,
            "household_members": household_members,
            "household_accounts": household_accounts,
            "current_user_id": current_user.id,
        },
        status_code=200,
    )


@router.post("/decrypt-statement", response_class=HTMLResponse)
async def decrypt_statement(
    request: Request,
    file_id: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Tenta descriptografar um PDF salvo temporariamente com a senha fornecida."""
    temp_path = PENDING_DIR / file_id
    if not temp_path.exists():
        return templates.TemplateResponse(
            request,
            "partials/password_prompt.html",
            {
                "file_id": file_id,
                "filename": "arquivo não encontrado",
                "error": True,
                "error_message": "Arquivo expirado ou não encontrado. Faça o upload novamente.",
            },
        )

    file_bytes = temp_path.read_bytes()

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:
        temp_path.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request,
            "partials/password_prompt.html",
            {
                "file_id": file_id,
                "filename": "arquivo corrompido",
                "error": True,
                "error_message": "Arquivo corrompido. Faça o upload novamente.",
            },
        )

    try:
        if doc.is_encrypted:
            success = doc.authenticate(password)
            if not success:
                return templates.TemplateResponse(
                    request,
                    "partials/password_prompt.html",
                    {
                        "file_id": file_id,
                        "filename": "...",
                        "error": True,
                        "error_message": "Senha inválida. Os bancos costumam usar o CPF (apenas números) ou os 4 primeiros dígitos do cartão.",
                    },
                )
    finally:
        try:
            doc.close()
        except Exception:
            pass

    # Password correct — extract text and proceed with AI
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if doc.is_encrypted:
            doc.authenticate(password)
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text("text"))
        text = "\n".join(text_parts)
        doc.close()
    except Exception:
        return templates.TemplateResponse(
            request,
            "partials/password_prompt.html",
            {
                "file_id": file_id,
                "filename": "...",
                "error": True,
                "error_message": "Erro ao processar o arquivo após descriptografia.",
            },
        )

    # Clean up temp file
    temp_path.unlink(missing_ok=True)

    # Run AI analysis (same logic as upload_statement)
    household_id = current_user.household_id
    categories = db.exec(
        select(Category).where(
            (Category.household_id == household_id) | (Category.household_id.is_(None))
        )
    ).all()
    category_names = [c.name for c in categories]
    household_members = db.exec(
        select(User).where(User.household_id == household_id)
    ).all()
    household_accounts = db.exec(
        select(Account).where(Account.household_id == household_id)
    ).all()

    try:
        analysis = await ai_service.analyze_financial_text(text, category_names)
    except Exception:
        return templates.TemplateResponse(
            request,
            "partials/password_prompt.html",
            {
                "file_id": file_id,
                "filename": "...",
                "error": True,
                "error_message": "Falha na análise de IA. Tente novamente.",
            },
        )

    if not analysis.documento_valido:
        return templates.TemplateResponse(
            request,
            "partials/password_prompt.html",
            {
                "file_id": file_id,
                "filename": "...",
                "error": True,
                "error_message": analysis.codigo_erro or "Documento inválido",
            },
        )

    doc_type = analysis.tipo_documento or None
    bank_name = analysis.banco_identificado
    bank_slug = _normalize_bank_slug(bank_name)
    last_4 = analysis.quatro_ultimos_digitos
    saldo_final = analysis.saldo_final_extrato
    transactions = [t.model_dump() for t in analysis.transacoes]

    # Correct transaction dates using deterministic year deduction
    if doc_type == "FATURA":
        for tx in transactions:
            tx["data"] = _build_transaction_date(
                tx,
                invoice_month=analysis.mes_vencimento,
                invoice_year=analysis.ano_vencimento,
            )
            # Force FATURA expenses to be negative (AI sometimes returns positive)
            desc = str(tx.get("description_clean") or tx.get("descricao", "")).strip()
            is_refund = _is_credit_card_refund(desc)
            if not is_refund:
                try:
                    val = float(tx.get("valor", 0))
                    if val > 0:
                        tx["valor"] = -val
                except (ValueError, TypeError):
                    pass

    account = _match_account(db, household_id, bank_slug, last_4, doc_type)
    account_exists = account is not None

    file_result = {
        "filename": "...",
        "doc_type": doc_type,
        "bank_name": bank_name,
        "bank_slug": bank_slug,
        "last_4": last_4,
        "saldo_final": saldo_final,
        "mes_vencimento": analysis.mes_vencimento,
        "ano_vencimento": analysis.ano_vencimento,
        "account_id": str(account.id) if account else None,
        "account_name": account.name if account else None,
        "account_exists": account_exists,
        "transactions": transactions,
        "error": None,
    }

    return templates.TemplateResponse(
        request,
        "partials/single_file_card.html",
        {
            "f": file_result,
            "categories": categories,
            "household_members": household_members,
            "household_accounts": household_accounts,
            "current_user_id": current_user.id,
        },
    )


async def _process_file_import_background(file_import_id: UUID, household_id: UUID) -> None:
    try:
        with Session(engine) as db:
            await process_file_import_payload(db, household_id, file_import_id)
    except Exception as err:
        with Session(engine) as db:
            file_import = db.exec(
                select(FileImport).where(
                    FileImport.id == file_import_id,
                    FileImport.household_id == household_id,
                )
            ).first()
            if file_import:
                file_import.status = FILE_IMPORT_STATUS_FAILED
                file_import.progress_message = "Erro no processamento"
                file_import.error_message = str(err)
                db.add(file_import)
                db.commit()


@router.get("/upload-statement/status")
async def upload_statement_status(
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    file_imports = db.exec(
        select(FileImport)
        .where(FileImport.household_id == current_user.household_id)
        .order_by(FileImport.uploaded_at.desc())
    ).all()
    return [
        {
            "id": str(fi.id),
            "display_name": fi.display_name,
            "status": fi.status,
            "progress_message": fi.progress_message,
            "error_message": fi.error_message,
            "uploaded_at": fi.uploaded_at.isoformat() if fi.uploaded_at else None,
        }
        for fi in file_imports
        if fi.status != FILE_IMPORT_STATUS_DONE or fi.payload
    ]


@router.post("/upload-statement/confirm", response_class=HTMLResponse)
async def confirm_transactions(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    import json
    form = await request.form()
    raw_files = form.get("files", "[]")
    if isinstance(raw_files, str):
        try:
            raw_files = json.loads(raw_files)
        except Exception:
            raw_files = []

    file_imports_started = 0
    import_ids_info = []

    for file_data in raw_files:
        account_id_str = file_data.get("account_id")
        filename = file_data.get("filename", "documento")
        bank_name = file_data.get("bank_name", "")
        doc_type = file_data.get("doc_type")
        extraction_import_id = file_data.get("extraction_import_id")

        if not account_id_str and file_data.get("bank_slug") and doc_type:
            bank_display = file_data.get("bank_slug").capitalize()
            account_type = DOC_TYPE_ACCOUNT_TYPE.get(doc_type, "CORRENTE")
            name = f"{bank_display} {'****' + (file_data.get('last_4') or '') if file_data.get('last_4') else account_type}"
            new_account = Account(
                name=name.strip(),
                type=account_type,
                bank_slug=file_data.get("bank_slug") or None,
                last_4_digits=file_data.get("last_4") or None,
                balance=0.0,
                credit_limit=None,
                user_id=current_user.id,
                household_id=current_user.household_id,
            )
            db.add(new_account)
            db.flush()
            db.refresh(new_account)
            account_id_str = str(new_account.id)

        file_import = None
        if extraction_import_id:
            try:
                file_import = db.exec(
                    select(FileImport).where(
                        FileImport.id == UUID(extraction_import_id),
                        FileImport.household_id == current_user.household_id,
                    )
                ).first()
            except Exception:
                file_import = None

        if not account_id_str:
            if file_import:
                file_import.status = FILE_IMPORT_STATUS_FAILED
                file_import.progress_message = "Conta ausente"
                file_import.error_message = "Nenhuma conta selecionada para o arquivo"
                db.add(file_import)
                db.commit()
            else:
                file_import = create_file_import(
                    db,
                    filename=filename,
                    bank_name=bank_name or None,
                    household_id=current_user.household_id,
                    doc_type=doc_type,
                    payload=json.dumps(file_data, default=str),
                )
                file_import.status = FILE_IMPORT_STATUS_FAILED
                file_import.progress_message = "Conta ausente"
                file_import.error_message = "Nenhuma conta selecionada para o arquivo"
                db.add(file_import)
                db.commit()
            continue

        try:
            UUID(account_id_str)
        except Exception:
            if file_import:
                file_import.status = FILE_IMPORT_STATUS_FAILED
                file_import.progress_message = "Conta inválida"
                file_import.error_message = "ID de conta inválido"
                db.add(file_import)
                db.commit()
            else:
                file_import = create_file_import(
                    db,
                    filename=filename,
                    bank_name=bank_name or None,
                    household_id=current_user.household_id,
                    doc_type=doc_type,
                    payload=json.dumps(file_data, default=str),
                )
                file_import.status = FILE_IMPORT_STATUS_FAILED
                file_import.progress_message = "Conta inválida"
                file_import.error_message = "ID de conta inválido"
                db.add(file_import)
                db.commit()
            continue

        if not file_import:
            file_import = create_file_import(
                db,
                filename=filename,
                bank_name=bank_name or None,
                household_id=current_user.household_id,
                doc_type=doc_type,
                payload=json.dumps(file_data, default=str),
            )

        file_import.status = FILE_IMPORT_STATUS_PENDING
        file_import.progress_message = "Aguardando processamento"
        file_import.error_message = None
        file_import.payload = json.dumps(file_data, default=str)
        if bank_name:
            file_import.display_name = f"{bank_name} - {_doc_type_label(doc_type)} - {datetime.utcnow().strftime('%d/%m/%Y')}"
        db.add(file_import)
        db.commit()
        db.refresh(file_import)

        background_tasks.add_task(_process_file_import_background, file_import.id, current_user.household_id)
        file_imports_started += 1
        import_ids_info.append({"id": str(file_import.id), "display_name": file_import.display_name, "status": "PENDING"})

    response_headers = {}
    if import_ids_info:
        response_headers["X-Import-IDs"] = json.dumps(import_ids_info)

    return HTMLResponse(
        content=f"""
        <div id=\"main-upload-container\" class=\"space-y-6\">
            <div class=\"rounded-xl border border-emerald-200 bg-emerald-50 p-8 text-center shadow-sm\">
                <svg class=\"mx-auto h-12 w-12 text-emerald-500\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"2\" d=\"M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z\"/></svg>
                <h2 class=\"mt-4 text-lg font-semibold text-emerald-800\">Importação iniciada</h2>
                <p class=\"mt-1 text-sm text-emerald-600\">{file_imports_started} arquivo{'s' if file_imports_started != 1 else ''} enviado{'s' if file_imports_started != 1 else ''}. O processamento continuará em segundo plano.</p>
                <p class=\"mt-2 text-xs text-slate-500\">Você pode ver o histórico de processamento na página de extrato.</p>
                <div class=\"mt-6 flex items-center justify-center gap-3\">
                    <a href=\"/extrato\" class=\"inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-emerald-700\">
                        Ver Extrato
                    </a>
                    <button hx-get=\"/upload\" hx-target=\"#main-upload-container\"
                            class=\"inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-5 py-2.5 text-sm font-medium text-slate-600 shadow-sm transition-colors hover:bg-slate-50\">
                        Importar outro extrato
                    </button>
                </div>
            </div>
        </div>
        """,
        headers=response_headers,
    )


@router.post("/upload-statement/quick-create-account")
async def quick_create_account(
    request: Request,
    bank_slug: str = Form(...),
    last_4: str = Form(""),
    doc_type: str = Form(""),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    from uuid import uuid4

    account_type = DOC_TYPE_ACCOUNT_TYPE.get(doc_type, "CORRENTE")
    bank_display = bank_slug.capitalize() if bank_slug else "Conta"
    name = f"{bank_display} {'****' + last_4 if last_4 else ''}"

    account = Account(
        name=name.strip(),
        type=account_type,
        bank_slug=bank_slug or None,
        last_4_digits=last_4 or None,
        balance=0.0,
        credit_limit=None,
        user_id=current_user.id,
        household_id=current_user.household_id,
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    return {"status": "success", "account_id": str(account.id), "account_name": account.name}

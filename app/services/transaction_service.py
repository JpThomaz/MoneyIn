import json
import time
from datetime import date, datetime, timedelta
from typing import List, Optional
from uuid import UUID

from sqlmodel import select, delete, Session

from app.core.database import engine
from app.models.domain import Transaction, Account, AccountBalanceHistory, FileImport
from app.services.crypto_service import generate_sha256, generate_transaction_hash

FILE_IMPORT_STATUS_PENDING = "PENDING"
FILE_IMPORT_STATUS_EXTRACTING = "EXTRACTING"
FILE_IMPORT_STATUS_PROCESSING = "PROCESSING"
FILE_IMPORT_STATUS_DONE = "DONE"
FILE_IMPORT_STATUS_FAILED = "FAILED"


def _is_credit_card_refund(description: str | None) -> bool:
    if not description:
        return False
    text = str(description).strip().lower()
    refund_keywords = [
        "estorno",
        "estornado",
        "reembolso",
        "reembolsado",
        "devolução",
        "devolucao",
        "devolvido",
    ]
    return any(keyword in text for keyword in refund_keywords)


def calculate_transaction_year(
    invoice_month: int,
    invoice_year: int,
    t_month: int,
    current_installment: int,
) -> int:
    """Determina o ano real de uma transação em fatura bancária.

    Faturas (especialmente Itaú) omitem o ano nas linhas de transação.
    O ano deve ser deduzido deterministicamente a partir do mês de
    referência da fatura e do número da parcela.

    Lógica:
    - Compra à vista (current_installment == 1):
        Se t_month > invoice_month → a compra ocorreu no mês anterior
        ao fechamento, portanto ano = invoice_year - 1.
        Ex: fatura de 01/2026 com compra em 29/12 → ano = 2025.
        Caso contrário → ano = invoice_year.

    - Compra parcelada (current_installment > 1):
        Calcula o mês em que a 1ª parcela foi cobrada subtraindo
        (current_installment - 1) meses da data de referência.
        Se t_month > mês da 1ª parcela → a compra ocorreu no ciclo
        anterior, ano = ano_primeira_parcela - 1.
        Caso contrário → ano = ano_primeira_parcela.

    Testes mentais:
      - Fatura 03/2026, parcela 3/12, transação 15/01:
        1ª parcela = 03/2026 - 2 meses = 01/2026
        t_month=01 <= 01 → year = 2026 ✓
      - Fatura 03/2026, parcela 1/12, transação 15/01:
        à vista (inst=1), t_month=01 <= 03 → year = 2026 ✓
      - Fatura 01/2026, parcela 1/1, transação 29/12:
        à vista, t_month=12 > 01 → year = 2025 ✓
      - Fatura 04/2026, parcela 2/10, transação 15/01:
        1ª parcela = 04/2026 - 1 mês = 03/2026
        t_month=01 <= 03 → year = 2026 ✓
      - Fatura 01/2027, parcela 6/12, transação 29/12:
        1ª parcela = 01/2027 - 5 meses = 08/2026
        t_month=12 > 08 → year = 2025 ✓
    """
    if current_installment == 1:
        if t_month > invoice_month:
            return invoice_year - 1
        return invoice_year

    months_back = current_installment - 1
    first_inst_month = invoice_month - months_back
    first_inst_year = invoice_year
    while first_inst_month < 1:
        first_inst_month += 12
        first_inst_year -= 1

    if t_month > first_inst_month:
        return first_inst_year - 1
    return first_inst_year


def _build_transaction_date(tx: dict, invoice_month: int | None, invoice_year: int | None) -> str:
    """Constrói a data real da transação no formato YYYY-MM-DD.

    Para FATURAs, usa transaction_day/transaction_month + cálculo
    determinístico do ano via calculate_transaction_year.
    Para EXTRATOs (ou dados incompletos), usa o campo `data` original.
    
    O invoice_month/year recebidos SÃO o mês de cobrança da parcela atual
    (mes_vencimento da fatura). Fatura maio/2026 → parcela atual cobrada em maio (5).
    """
    day = tx.get("transaction_day")
    t_month = tx.get("transaction_month")

    if day is not None and t_month is not None and invoice_month is not None and invoice_year is not None:
        installment_num = int(tx.get("installment_number", 1))
        
        # O mes_vencimento da fatura JA É o mês de cobrança da parcela atual.
        # Fatura maio/2026 com parcela 7/12 → parcela 7 cobrada EM MAIO (mês 5).
        # Não subtrair 1!
        year = calculate_transaction_year(invoice_month, invoice_year, t_month, installment_num)
        try:
            return date(year, t_month, day).isoformat()
        except (ValueError, OverflowError):
            pass

    fallback = tx.get("data", "")
    if fallback and fallback != "0000-00-00":
        return fallback
    return date(invoice_year or 2026, invoice_month or 1, 1).isoformat()


def _business_days_between(start: date, end: date) -> int:
    if end < start:
        return 9999
    days = 0
    cur = start
    while cur < end:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


async def detect_internal_transfers(session: Session, household_id: UUID, date_start: date, date_end: date) -> int:
    """Detecta e marca transferências internas entre contas do mesmo household.

    Retorna o número de pares marcados como transferência (cada par conta como 2).
    """
    statement = select(Transaction).where(
        Transaction.household_id == household_id,
        Transaction.date >= date_start,
        Transaction.date <= date_end,
        Transaction.is_transfer == False,
    )
    txs = session.exec(statement).all()

    exits = [t for t in txs if t.amount < 0]
    entries = [t for t in txs if t.amount > 0]

    marked = 0

    for out_tx in exits:
        target = abs(out_tx.amount)
        for in_tx in entries:
            if in_tx.is_transfer:
                continue
            if abs(in_tx.amount) != target:
                continue
            if in_tx.date < out_tx.date:
                continue
            if _business_days_between(out_tx.date, in_tx.date) <= 2:
                out_tx.is_transfer = True
                in_tx.is_transfer = True
                marked += 2
                break

    if marked > 0:
        session.commit()

    return marked


DOC_TYPE_LABEL = {
    "EXTRATO": "Extrato",
    "FATURA": "Fatura",
    "BENEFICIOS": "Benefícios",
    "BOLETO": "Boleto",
    "COMPROVANTE": "Comprovante",
}


def _doc_type_label(doc_type: str | None) -> str:
    if not doc_type:
        return ""
    return DOC_TYPE_LABEL.get(doc_type.upper(), doc_type.upper() if doc_type else "")


def create_file_import(
    session: Session,
    filename: str,
    bank_name: str | None,
    household_id: UUID,
    doc_type: str | None = None,
    payload: str | None = None,
) -> FileImport:
    """Cria um registro FileImport com display_name.

    O display_name segue o padrão:
      "{Banco Identificado} - {Tipo Documento} - {DD/MM/AAAA}"
    Ex: "Nubank - Fatura - 10/06/2026"
    """
    date_part = datetime.utcnow().strftime("%d/%m/%Y")
    parts = [p for p in [bank_name, _doc_type_label(doc_type), date_part] if p]
    display_name = " - ".join(parts) if parts else filename

    fi = FileImport(
        filename=filename,
        display_name=display_name,
        household_id=household_id,
        status=FILE_IMPORT_STATUS_PENDING,
        progress_message="Aguardando processamento",
        payload=payload,
    )
    session.add(fi)
    session.flush()
    return fi


async def process_file_import_payload(
    session: Session,
    household_id: UUID,
    file_import_id: UUID,
) -> None:
    """Processa o payload persistido do FileImport em segundo plano."""
    file_import = session.exec(
        select(FileImport).where(
            FileImport.id == file_import_id,
            FileImport.household_id == household_id,
        )
    ).first()
    if not file_import:
        return

    file_import.status = FILE_IMPORT_STATUS_PROCESSING
    file_import.progress_message = "Processando importação"
    file_import.error_message = None
    session.add(file_import)
    session.commit()

    try:
        payload = json.loads(file_import.payload or "{}")
    except Exception as err:
        file_import.status = FILE_IMPORT_STATUS_FAILED
        file_import.progress_message = "Payload inválido"
        file_import.error_message = str(err)
        session.add(file_import)
        session.commit()
        return

    account_id = payload.get("account_id")
    if not account_id:
        file_import.status = FILE_IMPORT_STATUS_FAILED
        file_import.progress_message = "Conta não informada"
        file_import.error_message = "O arquivo não possui conta associada."
        session.add(file_import)
        session.commit()
        return

    try:
        account_uuid = UUID(account_id)
    except Exception as err:
        file_import.status = FILE_IMPORT_STATUS_FAILED
        file_import.progress_message = "Conta inválida"
        file_import.error_message = f"ID de conta inválido: {account_id}"
        session.add(file_import)
        session.commit()
        return

    transactions = payload.get("transactions")
    if not isinstance(transactions, list) or len(transactions) == 0:
        file_import.status = FILE_IMPORT_STATUS_FAILED
        file_import.progress_message = "Sem transações"
        file_import.error_message = "Nenhuma transação encontrada no arquivo."
        session.add(file_import)
        session.commit()
        return

    doc_type = payload.get("doc_type")
    saved_count = 0
    try:
        if doc_type == "FATURA":
            saved_count = await save_invoice_transactions(
                session,
                household_id,
                account_uuid,
                transactions,
                file_import_id=file_import_id,
                doc_reference_month=int(payload.get("mes_vencimento")) if payload.get("mes_vencimento") else None,
                doc_reference_year=int(payload.get("ano_vencimento")) if payload.get("ano_vencimento") else None,
            )
        else:
            saved_count = await save_parsed_transactions(
                session,
                household_id,
                account_uuid,
                transactions,
                file_import_id=file_import_id,
            )
    except Exception as err:
        file_import.status = FILE_IMPORT_STATUS_FAILED
        file_import.progress_message = "Erro ao salvar transações"
        file_import.error_message = str(err)
        session.add(file_import)
        session.commit()
        return

    file_import.status = FILE_IMPORT_STATUS_DONE
    if saved_count == 0:
        file_import.progress_message = "Nenhuma transação importada"
    else:
        file_import.progress_message = f"{saved_count} transação{'s' if saved_count != 1 else ''} importada{'s' if saved_count != 1 else ''}"
    session.add(file_import)
    session.commit()

    if doc_type == "FATURA":
        bank_slug = payload.get("bank_slug")
        if bank_slug:
            try:
                await process_invoice_payments(session, household_id, bank_slug)
            except Exception:
                pass


async def save_parsed_transactions(
    session: Session,
    household_id,
    account_id,
    transactions_data: List[dict],
    file_import_id: UUID | None = None,
) -> int:
    """Salva transações parseadas no banco, ignorando duplicatas por transaction_hash e household_id.

    Retorna o número de registros efetivamente salvos.
    Também dispara detecção de transferências internas para as datas inseridas.
    """
    household_uuid = household_id if isinstance(household_id, UUID) else UUID(household_id)
    account_uuid = account_id if isinstance(account_id, UUID) else UUID(account_id)

    saved = 0
    saved_dates: List[date] = []

    for tx in transactions_data:
        date_str = tx.get("data")
        if not date_str:
            continue

        try:
            tx_date = date.fromisoformat(date_str)
        except Exception:
            try:
                tx_date = datetime.fromisoformat(date_str).date()
            except Exception:
                continue

        try:
            amount = float(tx.get("valor"))
        except Exception:
            continue

        description = tx.get("descricao", "")
        category_id = tx.get("category_id")

        tx_hash = generate_transaction_hash(date_str, amount, description, str(account_uuid))

        statement = select(Transaction).where(
            Transaction.transaction_hash == tx_hash,
            Transaction.household_id == household_uuid,
        )
        existing = session.exec(statement).first()
        if existing:
            continue

        new_tx = Transaction(
            date=tx_date,
            description=" ".join(str(description).strip().split()),
            amount=amount,
            account_id=account_uuid,
            category_id=UUID(category_id) if category_id else None,
            household_id=household_uuid,
            is_transfer=False,
            transaction_hash=tx_hash,
            file_import_id=file_import_id,
        )

        session.add(new_tx)
        saved += 1
        saved_dates.append(tx_date)

    if saved > 0:
        session.commit()

        try:
            date_start = min(saved_dates)
            date_end = max(saved_dates)
            await detect_internal_transfers(session, household_uuid, date_start, date_end)
        except Exception:
            pass

    return saved


def calcular_referencia_parcela(
    parcela_alvo: int,
    parcela_atual: int,
    mes_ref: int,
    ano_ref: int,
) -> tuple[int, int]:
    """Calcula o mês e ano de referência para QUALQUER parcela (passada, atual ou futura).

    Lógica:
      - diff = parcela_alvo - parcela_atual (negativo para passado, 0 para atual, positivo para futuro)
      - total_meses = (mes_ref - 1) + diff
      - mes = total_meses % 12 + 1
      - ano = ano_ref + (total_meses // 12)

    Ex: fatura maio/2026, parcela_atual=7, mes_ref=5, ano_ref=2026
        parcela_alvo=1 → diff=-6 → total_meses=4+(-6)=-2 → mes=11, ano=2025 ✓ (Nov/2025)
        parcela_alvo=4 → diff=-3 → total_meses=4+(-3)=1  → mes=2,  ano=2026 ✓ (Fev/2026)
        parcela_alvo=11→ diff=+4 → total_meses=4+4=8     → mes=9,  ano=2026 ✓ (Set/2026)
    """
    diff = parcela_alvo - parcela_atual
    total_meses = (mes_ref - 1) + diff
    mes = total_meses % 12 + 1
    ano = ano_ref + (total_meses // 12)
    return mes, ano


async def save_invoice_transactions(
    session: Session,
    household_id: UUID,
    account_id: UUID,
    transactions_data: List[dict],
    file_import_id: UUID | None = None,
    doc_reference_month: int | None = None,
    doc_reference_year: int | None = None,
) -> int:
    """Salva transações de FATURA com parcelamento matemático puro.

    Algoritmo hash_pai + geração sequencial de parcelas:
     1. HASH PAI = sha256(descricao_valor_totalParcelas_conta) ancora todo o contrato
     2. Parcela atual é salva como CONFIRMED com reference_month = mês da fatura
     3. Parcelas futuras são PROJECTED com número sequencial estritamente matemático
     4. Cada parcela tem hash = sha256(hash_pai_numeroParcela)
     5. Verificação de duplicidade por hash ANTES de inserir
     6. Mês/ano futuro calculado sem depender de calendário: total_meses = (mes_ref - 1) + i
    """
    saved = 0

    for tx in transactions_data:
        date_str = tx.get("data")
        if not date_str:
            continue

        try:
            tx_date = date.fromisoformat(date_str)
        except Exception:
            try:
                tx_date = datetime.fromisoformat(date_str).date()
            except Exception:
                continue

        description_raw = str(tx.get("descricao", "")).strip()
        description_clean = str(tx.get("description_clean") or description_raw).strip()
        is_refund = _is_credit_card_refund(description_clean)
        try:
            amount = abs(float(tx.get("valor", 0)))
        except Exception:
            continue
        if not is_refund:
            amount = -amount

        category_id = tx.get("category_id")
        user_id = tx.get("user_id")

        installment_num = int(tx.get("installment_number", 1))
        total_ins = int(tx.get("total_installments", 1))
        is_installment = total_ins > 1

        ref_month = doc_reference_month
        ref_year = doc_reference_year
        if ref_month is None or ref_year is None:
            ref_month = ref_month or tx.get("reference_month")
            ref_year = ref_year or tx.get("reference_year")
        if ref_month is None or ref_year is None:
            print(f"  WARN: ref_month/year missing for '{description_clean}', saving as single purchase")
            total_ins = 1
            installment_num = 1
            is_installment = False

        print(f"  DEBUG: '{description_clean}' ref={ref_month}/{ref_year} inst={installment_num}/{total_ins} date={tx_date}")
        # 1. HASH PAI - único para o contrato inteiro
        hash_pai = generate_sha256(
            str(description_clean), str(abs(amount)), str(total_ins), str(account_id),
        )

        today = date.today()

        # O mes_vencimento da fatura (ref_month) JÁ É o mês de cobrança
        # da parcela atual. Não subtrair 1!
        # Ex: fatura maio/2026, parcela 7/12 → ref_month=5, ref_year=2026.
        #     parcela 7 cobrada em MAIO (mês 5).

        if is_installment:
            # 2. Gera as PARCELAS PASSADAS (1 até N-1) como CONFIRMED
            for past_i in range(1, installment_num):
                mes_past, ano_past = calcular_referencia_parcela(past_i, installment_num, ref_month, ref_year)
                hash_past = generate_sha256(str(hash_pai), str(past_i))

                existing_past = session.exec(
                    select(Transaction).where(
                        Transaction.transaction_hash == hash_past,
                        Transaction.household_id == household_id,
                    )
                ).first()

                if existing_past:
                    if existing_past.status == "PROJECTED":
                        existing_past.status = "CONFIRMED"
                    if existing_past.reference_month != mes_past or existing_past.reference_year != ano_past:
                        existing_past.reference_month = mes_past
                        existing_past.reference_year = ano_past
                    session.add(existing_past)
                    saved += 1
                    continue

                past_tx = Transaction(
                    date=date(ano_past, mes_past, 1),
                    description=description_clean,
                    amount=amount,
                    account_id=account_id,
                    category_id=UUID(category_id) if category_id else None,
                    user_id=UUID(user_id) if user_id else None,
                    household_id=household_id,
                    is_transfer=False,
                    transaction_hash=hash_past,
                    file_import_id=file_import_id,
                    installment_number=past_i,
                    total_installments=total_ins,
                    reference_month=mes_past,
                    reference_year=ano_past,
                    status="CONFIRMED",
                )
                session.add(past_tx)
                saved += 1

            # 3. Salva a PARCELA ATUAL como CONFIRMED
            # A parcela atual tem reference_month = ref_month (mês de cobrança = mes_vencimento)
            hash_atual = generate_sha256(str(hash_pai), str(installment_num))

            stmt = select(Transaction).where(
                Transaction.transaction_hash == hash_atual,
                Transaction.household_id == household_id,
            )
            existing = session.exec(stmt).first()

            if existing:
                if existing.status == "PROJECTED":
                    existing.status = "CONFIRMED"
                    existing.date = tx_date
                    existing.description = description_clean
                    if category_id:
                        existing.category_id = UUID(category_id) if isinstance(category_id, str) else category_id
                    if user_id:
                        existing.user_id = UUID(user_id) if isinstance(user_id, str) else user_id
                if existing.reference_month != ref_month or existing.reference_year != ref_year:
                    existing.reference_month = ref_month
                    existing.reference_year = ref_year
                session.add(existing)
                saved += 1
            else:
                new_tx = Transaction(
                    date=tx_date,
                    description=description_clean,
                    amount=amount,
                    account_id=account_id,
                    category_id=UUID(category_id) if category_id else None,
                    user_id=UUID(user_id) if user_id else None,
                    household_id=household_id,
                    is_transfer=False,
                    transaction_hash=hash_atual,
                    file_import_id=file_import_id,
                    installment_number=installment_num,
                    total_installments=total_ins,
                    reference_month=ref_month,
                    reference_year=ref_year,
                    status="CONFIRMED",
                )
                session.add(new_tx)
                saved += 1

            # 4. Gera as PARCELAS FUTURAS (N+1 até M)
            # Base: ref_month/ref_year (mês de cobrança da parcela atual = mes_vencimento)
            parcelas_restantes = total_ins - installment_num

            for i in range(1, parcelas_restantes + 1):
                numero_proxima_parcela = installment_num + i
                mes_futuro, ano_futuro = calcular_referencia_parcela(numero_proxima_parcela, installment_num, ref_month, ref_year)

                hash_futuro = generate_sha256(str(hash_pai), str(numero_proxima_parcela))

                existing = session.exec(
                    select(Transaction).where(
                        Transaction.transaction_hash == hash_futuro,
                        Transaction.household_id == household_id,
                    )
                ).first()

                # Determina status: se o mês/ano futuro já passou, é CONFIRMED
                ref_date_futuro = date(ano_futuro, mes_futuro, 1)
                is_past = ref_date_futuro < date(today.year, today.month, 1)
                future_status = "CONFIRMED" if is_past else "PROJECTED"
                future_date = date(ano_futuro, mes_futuro, 1)

                if existing:
                    if existing.status != future_status:
                        existing.status = future_status
                    if existing.reference_month != mes_futuro or existing.reference_year != ano_futuro:
                        existing.reference_month = mes_futuro
                        existing.reference_year = ano_futuro
                    if is_past:
                        existing.date = future_date
                    session.add(existing)
                    continue

                projected_tx = Transaction(
                    date=future_date,
                    description=description_clean,
                    amount=amount,
                    account_id=account_id,
                    category_id=UUID(category_id) if category_id else None,
                    user_id=UUID(user_id) if user_id else None,
                    household_id=household_id,
                    is_transfer=False,
                    transaction_hash=hash_futuro,
                    file_import_id=file_import_id,
                    installment_number=numero_proxima_parcela,
                    total_installments=total_ins,
                    reference_month=mes_futuro,
                    reference_year=ano_futuro,
                    status=future_status,
                )
                session.add(projected_tx)
                saved += 1
            # --- COMPRA À VISTA ---
            tx_hash = generate_transaction_hash(date_str, amount, description_clean, str(account_id))

            existing = session.exec(
                select(Transaction).where(
                    Transaction.transaction_hash == tx_hash,
                    Transaction.household_id == household_id,
                )
            ).first()

            if existing:
                if existing.status == "PROJECTED":
                    existing.status = "CONFIRMED"
                    if existing.reference_month != ref_month or existing.reference_year != ref_year:
                        existing.reference_month = ref_month
                        existing.reference_year = ref_year
                    session.add(existing)
                    saved += 1
                continue

            new_tx = Transaction(
                date=tx_date,
                description=description_clean,
                amount=amount,
                account_id=account_id,
                category_id=UUID(category_id) if category_id else None,
                user_id=UUID(user_id) if user_id else None,
                household_id=household_id,
                is_transfer=False,
                transaction_hash=tx_hash,
                file_import_id=file_import_id,
                installment_number=1,
                total_installments=1,
                reference_month=ref_month,
                reference_year=ref_year,
                status="CONFIRMED",
            )
            session.add(new_tx)
            saved += 1

    if saved > 0:
        session.commit()

    return saved


async def update_account_balance(session: Session, account_id: UUID, new_balance: float) -> None:
    account = session.exec(select(Account).where(Account.id == account_id)).first()
    if not account:
        return
    account.balance = new_balance
    session.add(account)
    history = AccountBalanceHistory(
        account_id=account_id,
        balance=new_balance,
    )
    session.add(history)
    session.commit()


async def process_invoice_payments(
    session: Session,
    household_id: UUID,
    bank_slug: str | None,
) -> int:
    """Intercepta transações 'Pagamento de Fatura' vindas de conta corrente,
    marca como is_transfer e abate o saldo do cartão de crédito correspondente.

    Retorna o número de pagamentos processados.
    """
    if not bank_slug:
        return 0

    stmt = select(Transaction).where(
        Transaction.household_id == household_id,
        Transaction.description.ilike("Pagamento de Fatura%"),
        Transaction.is_transfer == False,
    )
    payments = session.exec(stmt).all()

    credit_account = session.exec(
        select(Account).where(
            Account.household_id == household_id,
            Account.bank_slug == bank_slug,
            Account.type == "CREDITO",
        )
    ).first()

    processed = 0
    for tx in payments:
        tx.is_transfer = True
        session.add(tx)
        processed += 1

    if processed > 0 and credit_account:
        payment_total = abs(sum(tx.amount for tx in payments if tx.amount < 0))
        new_balance = min(0.0, (credit_account.balance or 0.0) + payment_total)
        credit_account.balance = new_balance
        session.add(credit_account)
        history = AccountBalanceHistory(
            account_id=credit_account.id,
            balance=new_balance,
        )
        session.add(history)

    if processed > 0:
        session.commit()

    return processed

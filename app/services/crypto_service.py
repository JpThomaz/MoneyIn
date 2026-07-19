import hashlib


def generate_sha256(*parts: str) -> str:
    key = "_".join(parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_transaction_hash(date_str: str, amount: float, description: str, account_id: str | None = None) -> str:
    """Gera um hash SHA256 para identificar uma transação de forma idempotente.

    - Limpa a descrição (trim + uppercase)
    - Usa o valor absoluto do montante
    - Concatena em: "{date_str}_{abs(amount)}_{cleaned_description}"
    - Se account_id for fornecido, inclui na chave
    - Retorna hex digest SHA256
    """
    if description is None:
        description = ""
    cleaned_description = " ".join(str(description).strip().upper().split())
    key = f"{date_str}_{abs(amount)}_{cleaned_description}"
    if account_id:
        key = f"{key}_{account_id}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_installment_hash(
    description_clean: str,
    amount: float,
    installment_number: int,
    total_installments: int,
    account_id: str,
    reference_month: int | None = None,
    reference_year: int | None = None,
) -> str:
    key = f"{description_clean}_{abs(amount)}_{installment_number}_{total_installments}_{account_id}"
    if reference_month is not None and reference_year is not None:
        key = f"{key}_{reference_month}_{reference_year}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

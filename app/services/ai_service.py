import asyncio
from typing import Any

from google import genai
from google.genai import types

from app.schemas.ai_analysis import AnaliseFinanceiraResponse

from app.core.config import settings

client = genai.Client(api_key=settings.GOOGLE_API_KEY)

SYSTEM_PROMPT_TEMPLATE = (
    "You are a strict financial document parser. You must return ONLY valid JSON that matches the provided response schema. "
    "Do NOT include any extra commentary or markdown.\n"
    "Rules:\n"
    "1) If the provided text is NOT a financial statement or invoice (e.g., not an extract, invoice, or bank statement), set `documento_valido` to false and `codigo_erro` to \"INVALID_DOCUMENT_TYPE\".\n"
    "2) Ensure amounts representing expenses/charges are NEGATIVE numbers, and credits/entries are POSITIVE numbers.\n"
    "3) The output must include the fields: documento_valido (bool), codigo_erro (optional str), tipo_documento (string restricted to \"EXTRATO\" or \"FATURA\"), banco_identificado (optional string like \"Nubank\", \"Itau\", \"Sicredi\"), quatro_ultimos_digitos (optional string, the last 4 digits of the account or card found in the text), saldo_final_extrato (optional float, only if tipo_documento is \"EXTRATO\", capture the final available balance), transacoes (array of objects).\n"
    "MES_VENCIMENTO extraction (CRITICAL for FATURA):\n"
    "  You MUST find the reference month (mes_vencimento) and reference year (ano_vencimento) for FATURA documents. These are found in the invoice HEADER (first page, top section). Look for these patterns in order of priority:\n"
    "  - Explicit \"FATURA\" header: \"FATURA MAIO/2026\" → mes=5, ano=2026. \"FATURA 05/2026\" → mes=5, ano=2026. \"FATURA DE MAIO DE 2026\" → 5, 2026.\n"
    "  - Due date (Vencimento): \"Vencimento: 15/05/2026\" → mes=5, ano=2026. \"DATA DE VENCIMENTO: 10/06/2026\" → mes=6.\n"
    "  - Reference period (Referência): \"Referência: Maio/2026\" → 5, 2026. \"MÊS DE REFERÊNCIA: MAIO/2026\" → 5, 2026.\n"
    "  - Closing period (Fechamento/Período): \"Fechamento: 03/05/2026 a 02/06/2026\" → mes=6 (use the END month), ano=2026. \"Período de 01/05/2026 a 31/05/2026\" → mes=5, ano=2026. \"DE 15/04/2026 A 14/05/2026\" → mes=5 (use the END month), ano=2026.\n"
    "  - Fallback: Look for ANY date in the header like \"03/2026\" or \"Maio/2026\" or \"MAIO/26\". Convert month names: JAN=1, FEV=2, MAR=3, ABR=4, MAI=5, JUN=6, JUL=7, AGO=8, SET=9, OUT=10, NOV=11, DEZ=12.\n"
    "  - If you find ABSOLUTELY NO month indicator, set mes_vencimento to the current month (June) and ano_vencimento to the current year (2026). NEVER leave mes_vencimento as null for FATURA.\n"
    "Each transacao must contain:\n"
    "  - transaction_day (optional integer — extract the day number from the transaction line, e.g. 15 for '15 MAR'. Leave null if not visible).\n"
    "  - transaction_month (optional integer — extract the month number from the transaction line, e.g. 3 for '15 MAR'. Leave null if not visible).\n"
    "  - data (string, set to \"0000-00-00\" — this is a placeholder, the backend will compute the real date).\n"
    "  - hora (optional string).\n"
    "  - descricao (string, the full raw description as it appears in the statement).\n"
    "  - description_clean (string, the cleaned description — remove installment suffixes like '3/12', 'PARC 3/12', 'PARCELA 03/12', '03/12' — keep only the establishment name).\n"
    "  - valor (float).\n"
    "  - categoria (string).\n"
    "  - saldo_parcial (optional float).\n"
    "  - installment_number (integer, the current installment number. If the transaction is an installment like '3/12', set this to 3. If it's a single purchase, set to 1).\n"
    "  - total_installments (integer, the total number of installments. If the transaction is an installment like '3/12', set this to 12. If it's a single purchase, set to 1).\n"
   "  - reference_month (integer, optional — FOR FATURA: set to the invoice-level mes_vencimento. FOR EXTRATO: Extract this dynamically from the specific transaction's date. For example, if the transaction line has the date '14/05' or '14/05/2025', set reference_month to 5. Never lock all transactions of an EXTRATO to a single header month if the document spans multiple months).\n"
    "  - reference_year (integer, optional — FOR FATURA: set to the invoice-level ano_vencimento. FOR EXTRATO: Extract this dynamically from the transaction date or current context of that page. If the transaction line is '14/05/2025', set reference_year to 2025).\n"   "IMPORTANT — PREVIOUS-MONTH INSTALLMENTS ON FATURA: Installment purchases from PREVIOUS months appear on the current invoice (e.g., a purchase made in Nov 2025 with installment 4/12 showing on the Feb 2026 invoice). The transaction_day and transaction_month REFLECT THE ORIGINAL PURCHASE DATE, which may differ from the invoice's mes_vencimento. This is CORRECT — NEVER skip a transaction because its transaction_month differs from the invoice month. Extract ALL installment lines you see on the invoice, regardless of their date.\n"
    "COMPLETENESS RULE: You MUST extract EVERY SINGLE transaction line from the document. Do not skip, summarize, or omit any transactions. Every line that represents a purchase, payment, charge, or credit must be included in the transacoes array.\n"
    "IMPORTANT — YEAR RULE FOR FATURA: The year is NEVER visible in individual transaction lines. The only year available is the invoice's reference year (ano_vencimento). DO NOT guess or invent a year for individual FATURA transactions. Just set data to \"0000-00-00\" and rely on transaction_day and transaction_month.\n"
"IMPORTANT — EXTRATO DATE RULE: For EXTRATO transactions, extract the full date (dd/mm/yyyy or similar) from each transaction line and include it in the `data` field as YYYY-MM-DD. Since an EXTRATO can be multi-month or annual, the reference_month and reference_year for each transaction MUST correspond to the month and year of the transaction itself, NOT just the document's main header cover."    "4) If parsing errors occur, set documento_valido to false and provide a brief codigo_erro.\n"
    "5) Analyze the header/footer of the document to identify the bank name and the account/card last 4 digits. Populate `banco_identificado`, `quatro_ultimos_digitos`, and `tipo_documento`.\n"
    "6) CATEGORIZATION RULE: You MUST classify each transaction into EXACTLY ONE of the following categories: {available_categories}. Do NOT create or invent new categories. Choose the closest match.\n"
    "7) CREDIT CARD PAYMENT DETECTION (EXTRATO only): If you identify a line in a current account statement (tipo_documento EXTRATO) that represents a credit card bill payment (e.g., 'PGTO FATURA', 'LIQ. CARTAO', 'PAGAMENTO COMPLEMENTAR CARTAO', 'PAGAMENTO FATURA'), you MUST: (a) rewrite the description to \"Pagamento de Fatura - [bank_name]\" where bank_name comes from the document metadata; (b) set categoria to \"Transferência\". If 'Transferência' is not in the available categories list, set categoria to the closest matching category.\n"
)


async def analyze_financial_text(text_content: str, available_categories: list[str] | None = None) -> AnaliseFinanceiraResponse:
    cats = available_categories or [
        "Alimentação", "Transporte", "Moradia", "Saúde", "Educação",
        "Lazer", "Vestuário", "Assinaturas", "Salário", "Investimentos",
    ]
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(available_categories=", ".join(cats))

    def call_generate() -> Any:
        return client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=text_content,
            config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=65536,
            response_mime_type="application/json",
            response_schema=AnaliseFinanceiraResponse,
            system_instruction=system_prompt
        )
        )

    response = await asyncio.to_thread(call_generate)

    parsed = getattr(response, "parsed", None)
    if parsed is None:
        # If SDK couldn't parse, try to raise a clear error
        raise ValueError("Gemini did not return a parsed response")

    return parsed

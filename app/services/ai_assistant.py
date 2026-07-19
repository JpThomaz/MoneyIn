import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, select
from sqlalchemy import func, or_, and_

from app.core.config import settings
from app.models.domain import Transaction, Category, Account, User

logger = logging.getLogger("uvicorn.error")

# Safe Gemini import — never breaks the app at module load
try:
    from google import genai
    from google.genai import types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.warning("google-genai SDK not installed. AI features will use fallback mode.")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _month_date_range(year: int, month: int):
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _effective_month_filters(household_id: UUID, month: int, year: int):
    start, end = _month_date_range(year, month)
    return [
        Transaction.household_id == household_id,
        Transaction.is_transfer == False,
        Transaction.status == "CONFIRMED",
        or_(
            and_(Transaction.reference_month == month, Transaction.reference_year == year),
            and_(Transaction.reference_month.is_(None), Transaction.date >= start, Transaction.date <= end),
        ),
    ]


# ---------------------------------------------------------------------------
# Context gathering — always works, no external dependencies
# ---------------------------------------------------------------------------

def gather_family_context(db: Session, household_id: UUID) -> Dict[str, Any]:
    """Compile consolidated financial data for a household."""
    today = date.today()
    y, m = today.year, today.month
    filters = _effective_month_filters(household_id, m, y)

    # 1. Account balances
    accounts = db.exec(select(Account).where(Account.household_id == household_id)).all()
    account_balances = []
    for a in accounts:
        account_balances.append({
            "name": a.name,
            "type": a.type,
            "balance": round(float(a.balance or 0.0), 2),
        })
    total_balance = round(sum(ab["balance"] for ab in account_balances), 2)

    # 2. Monthly income and expenses
    total_income = float(
        db.exec(
            select(func.coalesce(func.sum(Transaction.amount), 0))
            .where(*filters, Transaction.amount > 0)
        ).first() or 0.0
    )
    total_expense = float(
        db.exec(
            select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0))
            .where(*filters, Transaction.amount < 0)
        ).first() or 0.0
    )

    # 3. Top 5 expense categories
    cat_rows = db.exec(
        select(
            func.coalesce(Category.name, "Outros").label("cat_name"),
            func.coalesce(func.sum(func.abs(Transaction.amount)), 0).label("total"),
        )
        .select_from(Transaction)
        .join(Category, Transaction.category_id == Category.id, isouter=True)
        .where(*filters, Transaction.amount < 0)
        .group_by("cat_name")
        .order_by(func.sum(func.abs(Transaction.amount)).desc())
        .limit(5)
    ).all()
    top_categories = [{"name": str(r[0]), "amount": round(float(r[1] or 0), 2)} for r in cat_rows]

    # 4. Projected installments for next 3 months
    projected = []
    for i in range(1, 4):
        pm = ((m + i - 1) % 12) + 1
        py = y + ((m + i - 1) // 12)
        pm_total = float(
            db.exec(
                select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
                    Transaction.household_id == household_id,
                    Transaction.is_transfer == False,
                    Transaction.status == "PROJECTED",
                    Transaction.reference_month == pm,
                    Transaction.reference_year == py,
                )
            ).first() or 0.0
        )
        if pm_total > 0:
            projected.append({"month": f"{pm:02d}/{py}", "amount": round(pm_total, 2)})

    # 5. Balance forecast (6-month projection)
    forecast_section = {}
    try:
        from app.api.routers.analytics import compute_balance_forecast
        fc = compute_balance_forecast(db, household_id, historical_months=6, forecast_months=6)
        forecast_values = fc.get("forecast_values", [])
        lower_bound = fc.get("lower_bound", [])
        upper_bound = fc.get("upper_bound", [])
        # Only include forecast months (skip None historical)
        forecast_months = [
            {
                "month": fc["labels"][i],
                "value": forecast_values[i],
                "lower": lower_bound[i],
                "upper": upper_bound[i],
            }
            for i in range(len(forecast_values))
            if forecast_values[i] is not None
        ]
        forecast_section = {
            "metodo": fc.get("method", "unknown"),
            "previsoes": forecast_months,
        }
    except Exception as exc:
        logger.debug("Forecast context skipped: %s", exc)

    return {
        "contas": account_balances,
        "saldo_total": total_balance,
        "receitas_mes": round(total_income, 2),
        "despesas_mes": round(total_expense, 2),
        "top_categorias_despesa": top_categories,
        "parcelas_projetadas_3meses": projected,
        "previsao_saldo": forecast_section,
        "tem_dados": total_income > 0 or total_expense > 0,
    }


# ---------------------------------------------------------------------------
# Insights generation — Gemini + robust fallback
# ---------------------------------------------------------------------------

def _get_fallback_insights(context: Dict[str, Any]) -> List[Dict[str, str]]:
    """Generate intelligent fallback insights from context data. Never returns empty."""
    saldo = context.get("saldo_total", 0)
    receitas = context.get("receitas_mes", 0)
    despesas = context.get("despesas_mes", 0)
    projetado = sum(p["amount"] for p in context.get("parcelas_projetadas_3meses", []))
    categorias = context.get("top_categorias_despesa", [])

    insights = []

    # Insight 1: Balance
    if saldo >= 0:
        insights.append({
            "type": "positive",
            "title": "Balanço Familiar Positivo",
            "text": f"O saldo consolidado de todas as contas é R$ {saldo:,.2f}. Continue assim!",
        })
    else:
        insights.append({
            "type": "warning",
            "title": "Saldo Negativo",
            "text": f"Atenção! O saldo consolidado está em R$ {saldo:,.2f}. Revise os gastos do mês.",
        })

    # Insight 2: Expense ratio
    if receitas > 0:
        ratio = (despesas / receitas) * 100
        if ratio > 90:
            insights.append({
                "type": "warning",
                "title": "Gasto Acima do Ideal",
                "text": f"Vocês já gastaram {ratio:.0f}% das receitas este mês. O ideal é manter abaixo de 80%.",
            })
        elif ratio < 60:
            insights.append({
                "type": "positive",
                "title": "Disciplina Financeira",
                "text": f"Excelente! Apenas {ratio:.0f}% da receita foi comprometida. Sobram R$ {receitas - despesas:,.2f}.",
            })
        else:
            insights.append({
                "type": "trend",
                "title": "Consumo Moderado",
                "text": f"{ratio:.0f}% da receita foi usada este mês. Monitor de perto os próximos dias.",
            })

    # Insight 3: Forecast or future commitments
    forecast = context.get("previsao_saldo", {})
    forecast_months = forecast.get("previsoes", [])
    if forecast_months:
        last_fc = forecast_months[-1]
        fc_val = last_fc.get("value", 0)
        if fc_val < 0:
            insights.append({
                "type": "warning",
                "title": "Alerta de Saldo Futuro",
                "text": f"A previsão indica saldo de R$ {fc_val:,.2f} em {last_fc['month']}. Reveja os gastos agora!",
            })
        elif fc_val > saldo * 1.1:
            insights.append({
                "type": "positive",
                "title": "Tendência de Crescimento",
                "text": f"O saldo deve crescer para R$ {fc_val:,.2f} em {last_fc['month']}. Continue assim!",
            })
        else:
            insights.append({
                "type": "trend",
                "title": "Previsão de Saldo",
                "text": f"Saldo projetado para {last_fc['month']}: R$ {fc_val:,.2f}. Mantenha a discipline.",
            })
    elif projetado > 0:
        insights.append({
            "type": "trend",
            "title": "Parcelas Futuras",
            "text": f"R$ {projetado:,.2f} estão comprometidos em parcelas projetadas nos próximos 3 meses.",
        })
    elif categorias:
        top = categorias[0]
        insights.append({
            "type": "trend",
            "title": "Maior Categoria de Gasto",
            "text": f"A maior despesa é {top['name']} com R$ {top['amount']:,.2f} este mês.",
        })

    # Ensure exactly 3 insights
    while len(insights) < 3:
        insights.append({
            "type": "trend",
            "title": "Oráculo Ativo",
            "text": "Configure sua GEMINI_API_KEY no .env para insights comportamentais ultra-personalizados.",
        })

    return insights[:3]


INSIGHTS_SYSTEM_PROMPT = (
    "Você é um analista financeiro pessoal de elite. Analise os dados fornecidos e gere "
    "exatamente 3 insights curtos, diretos e acionáveis para o casal.\n"
    "Formate a resposta estritamente em formato JSON contendo uma lista de objetos com os campos:\n"
    "- 'type': 'positive' (para conquistas ou economias), 'warning' (para gastos acima da média ou riscos) "
    "ou 'trend' (para projeções de parcelas futuras).\n"
    "- 'title': Um título curto e chamativo (máximo 5 palavras).\n"
    "- 'text': Uma frase explicativa e acionável.\n"
    "Se houver dados de previsão de saldo, inclua pelo menos 1 insight preventivo sobre a tendência "
    "do saldo nos próximos meses (alerta de déficit ou oportunidade de investimento).\n"
    "Evite jargões bancários e use um tom amigável.\n"
    "Responda APENAS com o JSON, sem markdown, sem explicação extra."
)


def get_insights(db: Session, household_id: UUID) -> List[Dict[str, str]]:
    """Get insights: try Gemini first, always fall back to context-based insights."""
    context = gather_family_context(db, household_id)

    # Try Gemini only if SDK available AND key configured
    if GEMINI_AVAILABLE and settings.GOOGLE_API_KEY:
        try:
            data = {
                "mes_atual": f"{date.today().month:02d}/{date.today().year}",
                "total_receitas": context.get("receitas_mes", 0),
                "total_despesas": context.get("despesas_mes", 0),
                "saldo_atual": context.get("saldo_total", 0),
                "top_categorias_despesa": context.get("top_categorias_despesa", []),
                "total_parcelas_futuras": sum(p["amount"] for p in context.get("parcelas_projetadas_3meses", [])),
                "previsao_saldo": context.get("previsao_saldo", {}),
            }
            user_prompt = f"Dados financeiros do mês:\n{json.dumps(data, ensure_ascii=False, indent=2)}"

            client = genai.Client(api_key=settings.GOOGLE_API_KEY)
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    max_output_tokens=1024,
                    system_instruction=INSIGHTS_SYSTEM_PROMPT,
                ),
            )
            raw_text = (response.text or "").strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()

            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                insights = []
                for item in parsed[:3]:
                    if isinstance(item, dict) and all(k in item for k in ("type", "title", "text")):
                        insights.append({
                            "type": str(item.get("type", "trend")),
                            "title": str(item.get("title", "")),
                            "text": str(item.get("text", "")),
                        })
                if len(insights) >= 2:
                    return insights
        except Exception as e:
            logger.warning(f"Gemini insights failed, using fallback: {e}")

    return _get_fallback_insights(context)


# ---------------------------------------------------------------------------
# Oracle chat — Gemini + robust fallback
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = (
    "Você é o 'Oráculo', um assistente financeiro de elite, bem-humorado, empático e focado "
    "em ajudar casais a gerenciarem suas finanças domésticas. Você tem acesso aos seguintes "
    "dados consolidados da família:\n\n{dados_consolidados}\n\n"
    "Responda de forma direta, clara, acolhedora e use formatação Markdown (como negritos e "
    "listas) para facilitar a leitura. Nunca invente dados que não estão no contexto fornecido. "
    "Se o usuário perguntar algo fora do contexto financeiro, traga-o de volta ao foco com sutileza."
)


def _get_chat_fallback(user_message: str, context: Dict[str, Any]) -> str:
    """Generate a context-aware fallback response when Gemini is unavailable."""
    msg = user_message.lower()
    saldo = context.get("saldo_total", 0)
    receitas = context.get("receitas_mes", 0)
    despesas = context.get("despesas_mes", 0)
    categorias = context.get("top_categorias_despesa", [])
    projetadas = context.get("parcelas_projetadas_3meses", [])
    forecast = context.get("previsao_saldo", {})
    forecast_months = forecast.get("previsoes", [])

    if any(w in msg for w in ["gasto", "despesa", "gastando", "consumo"]):
        cat_text = "\n".join(f"- **{c['name']}**: R$ {c['amount']:,.2f}" for c in categorias[:5]) if categorias else "Sem dados de categorias."
        return (
            f"📊 **Resumo de Gastos do Mês**\n\n"
            f"Total de despesas: **R$ {despesas:,.2f}**\n"
            f"Receitas: R$ {receitas:,.2f}\n\n"
            f"**Top categorias:**\n{cat_text}\n\n"
            f"Se quiser detalhes de alguma categoria, é só perguntar!"
        )

    if any(w in msg for w in ["parcela", "futuro", "próximo", "compromisso"]):
        if projetadas:
            proj_text = "\n".join(f"- {p['month']}: **R$ {p['amount']:,.2f}**" for p in projetadas)
            total_proj = sum(p["amount"] for p in projetadas)
            return (
                f"🔮 **Parcelas Projetadas**\n\n{proj_text}\n\n"
                f"Total comprometido: **R$ {total_proj:,.2f}**\n\n"
                f"Esses valores ainda não foram descontados. Planejem-se!"
            )
        return "✅ Não há parcelas projetadas para os próximos 3 meses. Boa notícia!"

    if any(w in msg for w in ["saldo", "conta", "dinheiro", "patrimônio"]):
        return (
            f"💰 **Saldo Consolidado**\n\n"
            f"O saldo total de todas as contas é **R$ {saldo:,.2f}**.\n\n"
            f"Se quiser ver o detalhamento por conta, é só pedir!"
        )

    if any(w in msg for w in ["gargalo", "problema", "ruim", "alerta"]):
        if despesas > 0 and receitas > 0:
            ratio = (despesas / receitas) * 100
            return (
                f"🔍 **Análise de Gargalo**\n\n"
                f"Este mês, vocês já comprometeram **{ratio:.0f}%** da receita.\n"
                f"Receitas: R$ {receitas:,.2f} | Despesas: R$ {despesas:,.2f}\n\n"
                f"{'⚠️ O gasto está alto! Revisem categorias maiores.' if ratio > 80 else '📊 O ritmo está dentro do aceitável.'}"
            )
        return "📊 Preciso de mais dados para identificar gargalos. Envie sua fatura ou extrato!"

    if any(w in msg for w in ["previsão", "previsao", "futuro saldo", "projeção", "projecao"]):
        if forecast_months:
            lines = []
            for fm in forecast_months[:4]:
                lines.append(f"- {fm['month']}: **R$ {fm['value']:,.2f}** (faixa: R$ {fm['lower']:,.2f} a R$ {fm['upper']:,.2f})")
            fc_text = "\n".join(lines)
            last = forecast_months[-1]
            trend = "queda" if last["value"] < saldo else "crescimento"
            return (
                f"📈 **Previsão de Saldo (6 meses)**\n\n{fc_text}\n\n"
                f"Tendência: **{trend}**. Método: {forecast.get('metodo', 'N/D')}."
            )
        return "📉 Sem dados históricos suficientes para gerar previsão. Registre mais transações!"

    # Default
    return (
        f"Olá! Sou o **Oráculo**. Seu saldo atual é **R$ {saldo:,.2f}** e as despesas do mês somam "
        f"**R$ {despesas:,.2f}**. Pergunte sobre gastos, parcelas, saldo ou qualquer coisa sobre as finanças!"
    )


async def ask_oracle(
    user_message: str,
    history: List[Dict[str, str]],
    db: Session,
    household_id: UUID,
) -> str:
    """Send message + context to Gemini and return the response text. Falls back gracefully."""
    context = gather_family_context(db, household_id)

    # Try Gemini if available
    if GEMINI_AVAILABLE and settings.GOOGLE_API_KEY:
        try:
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                dados_consolidados=json.dumps(context, ensure_ascii=False, indent=2)
            )
            contents = []
            for msg in history:
                role = "user" if msg.get("role") == "user" else "model"
                contents.append({"role": role, "parts": [msg.get("content", "")]})
            contents.append({"role": "user", "parts": [user_message]})

            client = genai.Client(api_key=settings.GOOGLE_API_KEY)
            response = await asyncio.to_thread(
                lambda: client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.5,
                        max_output_tokens=1024,
                        system_instruction=system_prompt,
                    ),
                )
            )
            if response.text:
                return response.text
        except Exception as e:
            logger.warning(f"Gemini chat failed, using fallback: {e}")

    return _get_chat_fallback(user_message, context)

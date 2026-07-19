import json
from typing import Dict, List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import Session
from starlette.responses import HTMLResponse

from app.core.database import get_session
from app.core.config import settings
from app.models.domain import User
from app.api.deps import get_current_user
from app.services.ai_assistant import ask_oracle

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# In-memory short-term conversation memory per household.
# Structure: { household_id_str: [ {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."} ] }
_conversation_store: Dict[str, List[Dict[str, str]]] = {}

MAX_HISTORY = 10


class ChatRequest(BaseModel):
    message: str


def _get_history(household_id: str) -> List[Dict[str, str]]:
    return _conversation_store.setdefault(household_id, [])


def _append_message(household_id: str, role: str, content: str):
    history = _get_history(household_id)
    history.append({"role": role, "content": content})
    # Trim to last MAX_HISTORY exchanges
    if len(history) > MAX_HISTORY:
        _conversation_store[household_id] = history[-MAX_HISTORY:]


@router.post("/chat/ask")
async def chat_ask(
    request: Request,
    message: str = Form(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Handle a chat message from the user, call Gemini, return rendered HTML partial."""
    hid = str(current_user.household_id)

    user_text = message.strip()
    if not user_text:
        return HTMLResponse("")

    # Append user message to history
    _append_message(hid, "user", user_text)
    history = _get_history(hid)

    # Determine if Gemini is configured
    if not settings.GOOGLE_API_KEY:
        ai_text = "O Oráculo está dormindo no momento. Verifique a chave da API do Gemini."
    else:
        try:
            ai_text = await ask_oracle(user_text, history, db, current_user.household_id)
        except Exception:
            ai_text = "Houve um problema ao conectar com o Oráculo. Tente novamente em instantes."

    _append_message(hid, "assistant", ai_text)

    content = templates.env.get_template("partials/chat_message.html").render(
        role="assistant", text=ai_text
    )
    return HTMLResponse(content)

"""Interpretación de mensajes con OpenAI (Responses API + Structured Outputs).

El agente solo interpreta: devuelve un ``AgentTurn``. No escribe en la base, no decide
folios ni confirma entregas; eso lo hace ``order_service``.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from app.config import Settings
from app.models import AgentTurn

logger = logging.getLogger("sales_bot.agent")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "sales_agent.md"
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
CALENDAR_DAYS = 21


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentContext:
    today: date
    timezone: str
    customer_name: str
    catalog: list[dict]
    order: dict | None  # pedido en curso, ya resumido para el modelo
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, texto), más antiguo primero
    message: str = ""

    def render(self) -> str:
        calendar = [
            f"{d.isoformat()} {WEEKDAYS[d.weekday()]}"
            for d in (self.today + timedelta(days=i) for i in range(CALENDAR_DAYS))
        ]
        return "\n".join(
            [
                "## Contexto",
                f"Fecha actual: {self.today.isoformat()} ({WEEKDAYS[self.today.weekday()]}), zona {self.timezone}.",
                "Calendario de referencia (hoy y siguientes días):",
                ", ".join(calendar),
                f"Cliente: {self.customer_name}",
                "Pedido en curso: "
                + (json.dumps(self.order, ensure_ascii=False) if self.order else "ninguno"),
                "Catálogo vigente:",
                json.dumps(self.catalog, ensure_ascii=False),
            ]
        )


class Agent(Protocol):
    def interpret(self, context: AgentContext) -> AgentTurn: ...


class OpenAISalesAgent:
    def __init__(self, *, api_key: str, model: str, timeout: float = 45.0):
        self.model = model
        self._client = OpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        self._instructions = PROMPT_PATH.read_text(encoding="utf-8")

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAISalesAgent | None":
        if settings.openai_api_key is None or settings.openai_model is None:
            return None
        return cls(api_key=settings.openai_api_key.get_secret_value(), model=settings.openai_model)

    def interpret(self, context: AgentContext) -> AgentTurn:
        conversation = [
            {"role": role, "content": text} for role, text in context.history if text
        ] + [{"role": "user", "content": context.message}]
        response = self._client.responses.parse(
            model=self.model,
            instructions=self._instructions + "\n\n" + context.render(),
            input=conversation,
            text_format=AgentTurn,
        )
        turn = response.output_parsed
        if turn is None:
            raise AgentError("El modelo no devolvió salida estructurada")
        usage = getattr(response, "usage", None)
        logger.info(
            "Agente: intent=%s items=%d fecha=%s tokens=%s/%s",
            turn.intent, len(turn.items), turn.requested_date,
            getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
        )
        return turn

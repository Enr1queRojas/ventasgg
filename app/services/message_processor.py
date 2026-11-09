"""Coordina el procesamiento de cada mensaje entrante.

1. Identifica al remitente (vendedor, desconocido, cliente).
2. Para clientes: arma el contexto y llama al agente de OpenAI (fuera de transacción).
3. Devuelve un paso ``commit(conn)`` que el worker ejecuta dentro de una transacción:
   aplica los cambios al pedido y produce las respuestas a encolar.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.db import repository
from app.integrations.openai_agent import Agent, AgentContext, OpenAISalesAgent
from app.models import Message, MessageDirection, MessageType
from app.phone import InvalidPhoneError, normalize_phone
from app.services import notification_service
from app.services.catalog_service import CatalogService
from app.services.order_service import OutgoingText, TurnApplier, summarize_order_for_agent

logger = logging.getLogger("sales_bot.processor")

Reply = OutgoingText
CommitStep = Callable[[sqlite3.Connection], list[Reply]]

HISTORY_LIMIT = 12
HISTORY_MAX_AGE = timedelta(hours=12)

_DEFAULT = object()
_agents: dict[tuple[str, str], Agent] = {}


def get_agent(settings: Settings) -> Agent | None:
    if settings.openai_api_key is None or settings.openai_model is None:
        return None
    key = (settings.openai_model, settings.openai_api_key.get_secret_value())
    if key not in _agents:
        _agents[key] = OpenAISalesAgent.from_settings(settings)
    return _agents[key]


def _history(conn: sqlite3.Connection, message: Message) -> list[tuple[str, str]]:
    cutoff = (datetime.now(UTC) - HISTORY_MAX_AGE).strftime("%Y-%m-%dT%H:%M:%S")
    return [
        ("user" if m.direction is MessageDirection.INBOUND else "assistant", m.text)
        for m in repository.recent_messages(conn, message.phone, HISTORY_LIMIT + 1)
        if m.id != message.id and m.text and m.created_at >= cutoff
    ][-HISTORY_LIMIT:]


def process_message(
    conn: sqlite3.Connection, settings: Settings, message: Message, agent: Agent | None | object = _DEFAULT
) -> list[Reply] | CommitStep:
    try:
        phone = normalize_phone(message.phone)
    except InvalidPhoneError:
        logger.warning("Mensaje %s con teléfono inválido; no se responde", message.id)
        return []

    if settings.sales_phone and phone == settings.sales_phone:
        return [Reply(message.phone, notification_service.sales_number_reply())]

    customer = repository.get_customer_by_phone(conn, phone)
    if customer is None:
        logger.info("Mensaje %s de número no registrado", message.id)
        return [Reply(message.phone, notification_service.unknown_customer_message(settings.sales_phone))]

    if message.message_type is MessageType.UNSUPPORTED or not message.text:
        return [Reply(message.phone, notification_service.unsupported_message_reply())]

    if agent is _DEFAULT:
        agent = get_agent(settings)
    if agent is None:
        return [Reply(message.phone, notification_service.provisional_customer_reply(customer))]

    catalog = CatalogService.from_db(conn)
    order = repository.get_active_order(conn, customer.id)
    today = datetime.now(settings.timezone).date()
    context = AgentContext(
        today=today,
        timezone=settings.tz,
        customer_name=customer.name,
        catalog=catalog.as_prompt_context(),
        order=summarize_order_for_agent(conn, order, catalog),
        history=_history(conn, message),
        message=message.text,
    )
    turn = agent.interpret(context)  # llamada de red: fuera de la transacción

    def commit(tx_conn: sqlite3.Connection) -> list[Reply]:
        return TurnApplier(
            tx_conn,
            customer=customer,
            reply_to=message.phone,
            catalog=CatalogService.from_db(tx_conn),
            today=today,
            sales_phone=settings.sales_phone,
            sales_template=settings.sales_notification_template,
            sales_template_language=settings.sales_notification_template_language,
        ).apply(turn)

    return commit

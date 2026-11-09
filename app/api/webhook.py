"""Webhook de Meta WhatsApp Cloud API.

GET  /webhook/whatsapp  -> verificación de suscripción (hub.verify_token / hub.challenge)
POST /webhook/whatsapp  -> eventos; se valida X-Hub-Signature-256, se guardan los mensajes
                           entrantes como ``pending`` y se responde 200 de inmediato.
                           El procesamiento lo hace el worker (etapa 8).
"""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from app.config import Settings
from app.db import repository
from app.db.database import open_db, transaction
from app.models import MessageType
from app.services.alerts import get_alerter

logger = logging.getLogger("sales_bot.webhook")

router = APIRouter(prefix="/webhook/whatsapp", tags=["webhook"])

# Tipos de Meta que se tratan como texto del cliente.
_TEXT_TYPES = {"text", "button", "interactive"}
# Tipos que no requieren respuesta (no se guardan ni se contestan).
_IGNORED_TYPES = {"reaction", "system", "ephemeral", "request_welcome"}


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    wa_id: str
    message_type: MessageType
    text: str | None
    meta_type: str


# --------------------------------------------------------------------------- helpers


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _extract_text(msg: dict[str, Any]) -> str | None:
    meta_type = msg.get("type")
    if meta_type == "text":
        return (msg.get("text") or {}).get("body")
    if meta_type == "button":
        return (msg.get("button") or {}).get("text")
    if meta_type == "interactive":
        interactive = msg.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title")
    return None


@dataclass(frozen=True)
class DeliveryStatus:
    message_id: str  # wamid del mensaje saliente
    status: str  # sent | delivered | read | failed
    error: str | None


def _relevant_values(payload: dict[str, Any], phone_number_id: str | None) -> list[dict[str, Any]]:
    if payload.get("object") != "whatsapp_business_account":
        return []
    values = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            target = (value.get("metadata") or {}).get("phone_number_id")
            if phone_number_id and target and target != phone_number_id:
                logger.info("Evento para otro phone_number_id (%s); se ignora", target)
                continue
            values.append(value)
    return values


def parse_statuses(payload: dict[str, Any], phone_number_id: str | None) -> list[DeliveryStatus]:
    """Avisos de entrega de mensajes salientes (Meta acepta el envío y luego informa el resultado)."""
    result = []
    for value in _relevant_values(payload, phone_number_id):
        for st in value.get("statuses") or []:
            if not st.get("id") or not st.get("status"):
                continue
            error = None
            if st.get("errors"):
                e = st["errors"][0]
                details = (e.get("error_data") or {}).get("details")
                error = f"Meta code={e.get('code')}: {e.get('title') or e.get('message')}" + (f" ({details})" if details else "")
            result.append(DeliveryStatus(st["id"], st["status"], error))
    return result


def parse_messages(payload: dict[str, Any], phone_number_id: str | None) -> list[InboundMessage]:
    """Extrae mensajes entrantes; descarta estados de entrega y eventos irrelevantes.

    Si ``phone_number_id`` está configurado, ignora eventos dirigidos a otro número
    de la misma cuenta de WhatsApp Business.
    """
    result: list[InboundMessage] = []
    for value in _relevant_values(payload, phone_number_id):
        for msg in value.get("messages") or []:
            message_id, wa_id, meta_type = msg.get("id"), msg.get("from"), msg.get("type")
            if not message_id or not wa_id or meta_type in _IGNORED_TYPES:
                continue
            text = _extract_text(msg) if meta_type in _TEXT_TYPES else None
            result.append(
                InboundMessage(
                    message_id=message_id,
                    wa_id=wa_id,
                    message_type=MessageType.TEXT if text else MessageType.UNSUPPORTED,
                    text=text,
                    meta_type=meta_type or "unknown",
                )
            )
    return result


def _persist(settings: Settings, messages: list[InboundMessage]) -> int:
    """Guarda los mensajes en una transacción. Devuelve cuántos eran nuevos."""
    new = 0
    with open_db(settings.database_path) as conn, transaction(conn):
        for m in messages:
            inserted = repository.insert_inbound_message(
                conn,
                whatsapp_message_id=m.message_id,
                phone=m.wa_id,
                text=m.text,
                message_type=m.message_type,
            )
            new += inserted is not None
    return new


def _record_statuses(settings: Settings, statuses: list[DeliveryStatus]) -> None:
    failed: list[tuple[str, str]] = []
    with open_db(settings.database_path) as conn, transaction(conn):
        for st in statuses:
            message_id = repository.find_outbound_by_wamid(conn, st.message_id)
            if message_id is None:
                continue  # p. ej. mensajes enviados desde el panel de Meta
            if st.status == "failed":
                error = st.error or "Meta informó fallo de entrega"
                repository.mark_delivery_failed(conn, message_id, error)
                logger.error("Meta no pudo entregar el mensaje %s: %s", message_id, st.error)
                phone = conn.execute("SELECT phone FROM messages WHERE id = ?", (message_id,)).fetchone()[0]
                failed.append((phone, error))
            else:
                logger.info("Mensaje %s: %s", message_id, st.status)
    alerter = get_alerter(settings)
    for phone, error in failed:  # fuera de la transacción: la alerta es una llamada de red
        hint = " (el destinatario no ha escrito en 24 h: se necesita plantilla)" if "131047" in error else ""
        alerter.send(
            f"delivery-failed:{phone}", "Mensaje no entregado",
            f"Meta no pudo entregar un mensaje a {phone}: {error[:300]}{hint}. Revisa el panel → Mensajes.",
            cooldown=15 * 60,
        )


# --------------------------------------------------------------------------- routes


@router.get("", response_class=PlainTextResponse)
def verify_subscription(
    request: Request,
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> str:
    settings: Settings = request.app.state.settings
    expected = settings.whatsapp_verify_token
    if expected is None:
        logger.error("WHATSAPP_VERIFY_TOKEN no está configurado; no se puede verificar el webhook")
        raise HTTPException(status_code=403)
    if mode != "subscribe" or not token or not challenge:
        raise HTTPException(status_code=403)
    if not hmac.compare_digest(token.encode(), expected.get_secret_value().encode()):
        logger.warning("Verificación de webhook rechazada: verify token incorrecto")
        raise HTTPException(status_code=403)
    logger.info("Webhook verificado por Meta")
    return challenge


@router.post("")
async def receive_event(request: Request) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    if settings.whatsapp_app_secret is None:
        logger.error("WHATSAPP_APP_SECRET no está configurado; se rechazan los eventos")
        raise HTTPException(status_code=503)

    body = await request.body()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret.get_secret_value()):
        logger.warning("Evento rechazado: firma X-Hub-Signature-256 inválida")
        raise HTTPException(status_code=401)

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400) from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400)

    messages = parse_messages(payload, settings.whatsapp_phone_number_id)
    statuses = parse_statuses(payload, settings.whatsapp_phone_number_id)
    wabas = ",".join(str(e.get("id")) for e in payload.get("entry") or [] if isinstance(e, dict))
    logger.info(
        "Evento de Meta recibido (WABA %s): %d mensaje(s) de cliente, %d aviso(s) de entrega",
        wabas or "-", len(messages), len(statuses),
    )
    if not messages and not statuses:
        return {"status": "ignored"}

    try:
        if statuses:
            await run_in_threadpool(_record_statuses, settings, statuses)
        if messages:
            new = await run_in_threadpool(_persist, settings, messages)
            logger.info("Mensajes recibidos: %d (nuevos: %d, duplicados: %d)", len(messages), new, len(messages) - new)
    except Exception:
        # Error 500 => Meta reintenta la entrega; no se pierde el evento.
        logger.exception("No se pudo guardar el evento de Meta")
        raise HTTPException(status_code=500) from None

    return {"status": "ok"}

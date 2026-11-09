"""Seguimiento de pedidos por ventas: programar entrega, marcar entregado o cancelar.

Lo usan el panel ``/admin`` y los comandos de WhatsApp del número de ventas. Cada acción
corre dentro de una transacción junto con el encolado del aviso al cliente, así que un
pedido nunca cambia de estado sin que su aviso quede en la cola (o al revés).
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from app.db import repository
from app.models import Order, OrderStatus
from app.services import notification_service as texts
from app.services.order_service import OutgoingText, TemplateMessage

logger = logging.getLogger("sales_bot.tracking")

OPEN_STATUSES = (OrderStatus.PENDING_SALES.value, OrderStatus.SCHEDULED.value)
NOTE_MAX = 300
_FOLIO = re.compile(r"^(?:#|ped(?:ido)?[\s\-_#]*)?0*(\d{1,6})$", re.IGNORECASE)


class OrderActionError(ValueError):
    """La acción no se puede aplicar (pedido inexistente, ya cerrado, fecha inválida...)."""


@dataclass(frozen=True)
class NotifyConfig:
    sales_phone: str | None = None
    template: str | None = None  # ORDER_UPDATE_TEMPLATE
    template_language: str = "es_MX"


@dataclass(frozen=True)
class ActionResult:
    order: Order
    summary: str  # para quien hizo el cambio
    outgoing: list[OutgoingText]


def parse_folio(text: str) -> str | None:
    """``PED-0001``, ``ped 1``, ``#1`` o ``1`` -> ``PED-0001``."""
    match = _FOLIO.match(text.strip())
    return f"PED-{int(match.group(1)):04d}" if match else None


def find_order(conn: sqlite3.Connection, reference: str) -> Order:
    folio = parse_folio(reference)
    order = repository.find_order_by_folio(conn, folio) if folio else None
    if order is None:
        raise OrderActionError(f"No encontré el pedido «{reference}».")
    return order


def _clean_note(note: str | None) -> str | None:
    note = " ".join((note or "").split())
    return note[:NOTE_MAX] or None


def _require_open(order: Order, action: str) -> None:
    if order.status.value not in OPEN_STATUSES:
        labels = {OrderStatus.CLOSED: "ya fue entregado", OrderStatus.CANCELLED: "está cancelado"}
        state = labels.get(order.status, "todavía no está registrado")
        raise OrderActionError(f"No se puede {action} el pedido {order.folio or order.id}: {state}.")


def _notice(conn, order: Order, text: str, detail: str, config: NotifyConfig) -> OutgoingText:
    customer = repository.get_customer(conn, order.customer_id)
    template = None
    if config.template:
        params = texts.order_update_params(customer.name, order.folio, detail)
        template = TemplateMessage(config.template, config.template_language, tuple(params))
    return OutgoingText(customer.phone, text, order.id, template)


def schedule_delivery(
    conn: sqlite3.Connection, order: Order, delivery: date, note: str | None, *,
    today: date, notify: bool, config: NotifyConfig,
) -> ActionResult:
    _require_open(order, "programar")
    if delivery < today:
        raise OrderActionError(f"La fecha de entrega ({delivery.strftime('%d/%m/%Y')}) ya pasó.")
    note = _clean_note(note)
    rescheduled = order.status is OrderStatus.SCHEDULED
    if not repository.transition_order(
        conn, order.id, from_statuses=OPEN_STATUSES, status=OrderStatus.SCHEDULED.value,
        delivery_date=delivery.isoformat(), sales_note=note,
    ):
        raise OrderActionError(f"El pedido {order.folio} cambió mientras tanto; vuelve a intentarlo.")
    updated = repository.get_order(conn, order.id)
    logger.info("Pedido %s programado para %s", order.folio, delivery.isoformat())
    outgoing = []
    if notify:
        text = texts.delivery_scheduled(order.folio, delivery, note, rescheduled=rescheduled)
        outgoing.append(_notice(conn, updated, text, texts.order_update_detail("scheduled", delivery=delivery, note=note), config))
    verb = "reprogramado" if rescheduled else "programado"
    return ActionResult(updated, f"Pedido {order.folio} {verb} para el {texts.format_date(delivery)}.", outgoing)


def mark_delivered(
    conn: sqlite3.Connection, order: Order, *, notify: bool, config: NotifyConfig, now: str | None = None
) -> ActionResult:
    _require_open(order, "marcar como entregado")
    if not repository.transition_order(
        conn, order.id, from_statuses=OPEN_STATUSES, status=OrderStatus.CLOSED.value,
        delivered_at=now or repository.utc_now(),
    ):
        raise OrderActionError(f"El pedido {order.folio} cambió mientras tanto; vuelve a intentarlo.")
    updated = repository.get_order(conn, order.id)
    logger.info("Pedido %s entregado", order.folio)
    outgoing = []
    if notify:
        outgoing.append(_notice(conn, updated, texts.order_delivered(order.folio), texts.order_update_detail("delivered"), config))
    return ActionResult(updated, f"Pedido {order.folio} marcado como entregado.", outgoing)


def cancel_order(
    conn: sqlite3.Connection, order: Order, reason: str | None, *,
    notify: bool, config: NotifyConfig, now: str | None = None,
) -> ActionResult:
    _require_open(order, "cancelar")
    reason = _clean_note(reason)
    if not repository.transition_order(
        conn, order.id, from_statuses=OPEN_STATUSES, status=OrderStatus.CANCELLED.value,
        cancelled_at=now or repository.utc_now(), sales_note=reason,
    ):
        raise OrderActionError(f"El pedido {order.folio} cambió mientras tanto; vuelve a intentarlo.")
    updated = repository.get_order(conn, order.id)
    logger.info("Pedido %s cancelado por ventas", order.folio)
    outgoing = []
    if notify:
        text = texts.order_cancelled_by_sales(order.folio, reason, config.sales_phone)
        outgoing.append(_notice(conn, updated, text, texts.order_update_detail("cancelled", note=reason), config))
    return ActionResult(updated, f"Pedido {order.folio} cancelado.", outgoing)

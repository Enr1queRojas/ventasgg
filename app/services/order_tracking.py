"""Seguimiento de pedidos por ventas desde el panel: programar entrega, marcar entregado o cancelar.

Solo cambia el estado del pedido; no se envía nada al cliente (ventas le confirma por su
cuenta). Las transiciones son condicionales para que dos cambios simultáneos no se pisen.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from app.db import repository
from app.models import Order, OrderStatus
from app.services import notification_service as texts

logger = logging.getLogger("sales_bot.tracking")

OPEN_STATUSES = (OrderStatus.PENDING_SALES.value, OrderStatus.SCHEDULED.value)
NOTE_MAX = 300
_FOLIO = re.compile(r"^(?:#|ped(?:ido)?[\s\-_#]*)?0*(\d{1,6})$", re.IGNORECASE)


class OrderActionError(ValueError):
    """La acción no se puede aplicar (pedido inexistente, ya cerrado, fecha inválida...)."""


@dataclass(frozen=True)
class ActionResult:
    order: Order
    summary: str  # para quien hizo el cambio


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


def _transition(conn: sqlite3.Connection, order: Order, status: OrderStatus, **fields: object) -> Order:
    if not repository.transition_order(conn, order.id, from_statuses=OPEN_STATUSES, status=status.value, **fields):
        raise OrderActionError(f"El pedido {order.folio} cambió mientras tanto; vuelve a intentarlo.")
    return repository.get_order(conn, order.id)


def schedule_delivery(
    conn: sqlite3.Connection, order: Order, delivery: date, note: str | None, *, today: date
) -> ActionResult:
    _require_open(order, "programar")
    if delivery < today:
        raise OrderActionError(f"La fecha de entrega ({delivery.strftime('%d/%m/%Y')}) ya pasó.")
    rescheduled = order.status is OrderStatus.SCHEDULED
    updated = _transition(conn, order, OrderStatus.SCHEDULED, delivery_date=delivery.isoformat(), sales_note=_clean_note(note))
    logger.info("Pedido %s programado para %s", order.folio, delivery.isoformat())
    verb = "reprogramado" if rescheduled else "programado"
    return ActionResult(updated, f"Pedido {order.folio} {verb} para el {texts.format_date(delivery)}.")


def mark_delivered(conn: sqlite3.Connection, order: Order, *, now: str | None = None) -> ActionResult:
    _require_open(order, "marcar como entregado")
    updated = _transition(conn, order, OrderStatus.CLOSED, delivered_at=now or repository.utc_now())
    logger.info("Pedido %s entregado", order.folio)
    return ActionResult(updated, f"Pedido {order.folio} marcado como entregado.")


def cancel_order(conn: sqlite3.Connection, order: Order, reason: str | None, *, now: str | None = None) -> ActionResult:
    _require_open(order, "cancelar")
    updated = _transition(
        conn, order, OrderStatus.CANCELLED, cancelled_at=now or repository.utc_now(), sales_note=_clean_note(reason)
    )
    logger.info("Pedido %s cancelado por ventas", order.folio)
    return ActionResult(updated, f"Pedido {order.folio} cancelado.")

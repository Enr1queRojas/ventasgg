"""Reglas del pedido: aplica la interpretación del agente al borrador, valida y decide la respuesta.

Todo lo de aquí se ejecuta dentro de la transacción del worker, junto con el encolado
de las respuestas, para que un fallo no deje el pedido y los mensajes desalineados.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from app.db import repository
from app.models import AgentIntent, AgentTurn, Customer, Order, OrderStatus, Unit
from app.services import notification_service as texts
from app.services.catalog_service import CatalogError, CatalogService, MatchStatus

logger = logging.getLogger("sales_bot.orders")

MAX_DAYS_AHEAD = 180

# Frases que el LLM no debe decir por su cuenta (registro, folios, promesas de entrega).
_UNSAFE_REPLY = re.compile(
    r"PED-\d|registr(é|e|ado|ada|amos)\b.*pedido|pedido.*\bregistrad|"
    r"(te\s+lo|lo)\s+(entregamos|entregaremos|llevamos|llevaremos)|llega(rá)?\s+el",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OutgoingText:
    phone: str
    text: str
    order_id: int | None = None


def safe_reply(text: str | None, fallback: str) -> str:
    text = (text or "").strip()
    if not text or _UNSAFE_REPLY.search(text):
        if text:
            logger.warning("Respuesta del modelo descartada por contener compromisos no permitidos")
        return fallback
    return text


def summarize_order_for_agent(conn: sqlite3.Connection, order: Order | None, catalog: CatalogService) -> dict | None:
    if order is None:
        return None
    items = []
    for item in repository.get_order_items(conn, order.id):
        product = repository.get_product(conn, item.product_id)
        items.append({"sku": product.sku, "name": product.name, "quantity": item.quantity, "unit": item.unit.value})
    return {
        "status": order.status.value,
        "items": items,
        "requested_date": order.requested_date.isoformat() if order.requested_date else None,
        "urgency": order.urgency.value if order.urgency else None,
        "notes": order.notes,
    }


def _summary_lines(conn: sqlite3.Connection, order_id: int) -> list[texts.SummaryLine]:
    return [
        texts.SummaryLine(repository.get_product(conn, i.product_id), i.quantity, i.unit)
        for i in repository.get_order_items(conn, order_id)
    ]


_UNIT_WORDS = {Unit.KG: "kilos", Unit.BULTOS: "bultos", Unit.PIEZAS: "piezas"}


def _units_text(units: tuple[Unit, ...]) -> str:
    return " o ".join(_UNIT_WORDS[u] for u in units)


class TurnApplier:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        customer: Customer,
        reply_to: str,
        catalog: CatalogService,
        today: date,
        sales_phone: str | None,
    ):
        self.conn = conn
        self.customer = customer
        self.reply_to = reply_to
        self.catalog = catalog
        self.today = today
        self.sales_phone = sales_phone

    def _say(self, text: str, order_id: int | None = None) -> list[OutgoingText]:
        return [OutgoingText(self.reply_to, text, order_id)]

    @staticmethod
    def _has_changes(turn: AgentTurn) -> bool:
        return bool(
            turn.items or turn.replace_all_items or turn.requested_date or turn.date_is_ambiguous
            or turn.urgency or turn.urgency_note or turn.notes
        )

    # ------------------------------------------------------------------ entrada

    def apply(self, turn: AgentTurn) -> list[OutgoingText]:
        order = repository.get_active_order(self.conn, self.customer.id)
        changes = self._has_changes(turn)

        if turn.intent is AgentIntent.CANCEL and not turn.items:
            if order is None:
                return self._say(texts.no_active_order())
            repository.update_order(self.conn, order.id, status=OrderStatus.CANCELLED.value)
            logger.info("Pedido %s cancelado por el cliente", order.id)
            return self._say(texts.order_cancelled(), order.id)

        if turn.intent is AgentIntent.CONFIRM and not changes and order is not None:
            if order.status is OrderStatus.AWAITING_CONFIRMATION:
                return self._register(order)
            return self._next_step(order, turn, changed=False)

        if not changes:
            fallback = "¿Qué producto y cantidad necesitas?" if order is None else self._missing_question(order)
            return self._say(safe_reply(turn.reply, fallback), order.id if order else None)

        if order is None:
            order = repository.create_draft_order(self.conn, self.customer.id)
        issues = self._apply_changes(order, turn)
        order = repository.get_order(self.conn, order.id)
        if order.status is OrderStatus.AWAITING_CONFIRMATION:
            # Cualquier cambio invalida el resumen presentado.
            repository.update_order(self.conn, order.id, status=OrderStatus.DRAFT.value)
            order = repository.get_order(self.conn, order.id)

        if issues:
            return self._say("\n".join(issues), order.id)
        return self._next_step(order, turn, changed=True)

    # ------------------------------------------------------------------ cambios

    def _apply_changes(self, order: Order, turn: AgentTurn) -> list[str]:
        issues: list[str] = []
        if turn.replace_all_items:
            repository.clear_order_items(self.conn, order.id)

        for item in turn.items:
            product = self.catalog.get_by_sku(item.sku) if item.sku else None
            if product is None:
                match = self.catalog.resolve(item.product_text)
                if match.status is MatchStatus.AMBIGUOUS:
                    options = " o ".join(p.name for p in match.candidates)
                    issues.append(f"¿Cuál «{item.product_text}» necesitas: {options}?")
                    continue
                if match.status is MatchStatus.NOT_FOUND:
                    names = ", ".join(p.name for p in self.catalog.products)
                    issues.append(f"No encuentro «{item.product_text}» en el catálogo. Manejamos: {names}.")
                    continue
                product = match.product

            if item.action == "remove":
                repository.remove_order_item(self.conn, order.id, product.id)
                continue

            unit = CatalogService.parse_unit(item.unit) if item.unit else None
            if unit is None and len(product.allowed_units) == 1:
                unit = product.allowed_units[0]
            if item.quantity is None:
                issues.append(f"¿Cuántos {_units_text(product.allowed_units)} de {product.name} necesitas?")
                continue
            if unit is None:
                issues.append(f"Los {item.quantity:g} de {product.name}, ¿son {_units_text(product.allowed_units)}?")
                continue
            try:
                line = self.catalog.validate_line(product.sku, item.quantity, unit)
            except CatalogError as exc:
                issues.append(str(exc))
                continue
            repository.set_order_item(self.conn, order.id, line.product.id, line.quantity, line.unit)

        fields: dict[str, object] = {}
        if turn.date_is_ambiguous and not turn.requested_date:
            issues.append("¿Me confirmas la fecha exacta en que lo necesitas? Por ejemplo: 10/11.")
        elif turn.requested_date:
            issue = self._check_date(turn.requested_date)
            if issue:
                issues.append(issue)
            else:
                fields["requested_date"] = turn.requested_date
        if turn.urgency:
            fields["urgency"] = turn.urgency
        if turn.urgency_note:
            fields["urgency_note"] = turn.urgency_note
        if turn.notes:
            fields["notes"] = f"{order.notes}; {turn.notes}" if order.notes else turn.notes
        repository.update_order(self.conn, order.id, **fields)
        return issues

    def _check_date(self, value: str) -> str | None:
        try:
            requested = date.fromisoformat(value)
        except ValueError:
            return "No entendí la fecha. ¿Para qué día lo necesitas?"
        if requested < self.today:
            return "Esa fecha ya pasó. ¿Para qué día lo necesitas?"
        if requested > self.today + timedelta(days=MAX_DAYS_AHEAD):
            return "Esa fecha está muy lejana. ¿Me indicas una fecha dentro de los próximos meses?"
        return None

    # ------------------------------------------------------------------ siguiente paso

    def _missing_question(self, order: Order) -> str:
        if not repository.get_order_items(self.conn, order.id):
            return "¿Qué producto y cantidad necesitas?"
        if order.requested_date is None:
            return "¿Para qué día lo necesitas?"
        return "¿Deseas agregar algo más o registro el pedido?"

    def _next_step(self, order: Order, turn: AgentTurn, *, changed: bool) -> list[OutgoingText]:
        lines = _summary_lines(self.conn, order.id)
        if lines and order.requested_date:
            repository.update_order(self.conn, order.id, status=OrderStatus.AWAITING_CONFIRMATION.value)
            order = repository.get_order(self.conn, order.id)
            return self._say(texts.order_summary(order, lines), order.id)

        question = self._missing_question(order)
        ack = texts.progress_ack(lines) if changed and turn.items else ""
        return self._say(f"{ack}\n\n{question}" if ack else question, order.id)

    def _register(self, order: Order) -> list[OutgoingText]:
        lines = _summary_lines(self.conn, order.id)
        if not lines or order.requested_date is None:
            repository.update_order(self.conn, order.id, status=OrderStatus.DRAFT.value)
            return self._say(self._missing_question(order), order.id)

        folio = repository.register_order(self.conn, order.id)
        if folio is None:  # ya no estaba esperando confirmación
            return self._say(texts.no_active_order())
        order = repository.get_order(self.conn, order.id)
        logger.info("Pedido %s registrado como %s", order.id, folio)

        out = self._say(texts.order_registered(folio), order.id)
        if self.sales_phone:
            out.append(OutgoingText(self.sales_phone, texts.sales_notification(folio, self.customer, order, lines), order.id))
        else:
            logger.warning("SALES_PHONE no configurado: no se avisa al vendedor del pedido %s", folio)
        return out

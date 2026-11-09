"""Comandos del número de ventas (``SALES_PHONE``): alta de clientes y consulta de pedidos.

Se interpretan con reglas fijas (sin LLM) para que una alta o baja nunca dependa de cómo
el modelo entienda el mensaje. Aceptan el comando corto o una frase normal («da de alta a
Juan Pérez con el teléfono 871…», «¿qué pedidos hay pendientes?», «ver el pedido 12»).
Los pedidos solo se consultan aquí; se gestionan en el panel.

    alta Juan Pérez 871 123 4567     registra (o reactiva y renombra) un cliente
    baja 871 123 4567                lo desactiva: deja de poder hacer pedidos
    clientes                         lista los clientes activos
    buscar juan                      busca por nombre o teléfono (incluye inactivos)
    pedidos                          resumen: pendientes y entregas programadas
    pedido 12                        detalle de un pedido (PED-0012)
    ayuda                            muestra estos comandos
"""

import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date

from app.db import repository
from app.models import Customer, Order, OrderStatus, Unit
from app.phone import InvalidPhoneError, format_phone_display, normalize_phone
from app.services import notification_service as texts
from app.services import order_tracking

logger = logging.getLogger("sales_bot.admin")

# WhatsApp corta los textos de más de 4096 caracteres; se deja margen.
MAX_REPLY_CHARS = 3500
LIST_LIMIT = 200

# Un teléfono: "+52 871 123 4567", "8711234567", "(871) 123-45-67"... al menos 10 dígitos.
_PHONE = r"\+?\(?\d[\d\s().\-]{8,}\d"
_ALTA_NAME_FIRST = re.compile(rf"^(?P<name>.+?)[\s,]+(?P<phone>{_PHONE})$")
_ALTA_PHONE_FIRST = re.compile(rf"^(?P<phone>{_PHONE})[\s,]+(?P<name>.+)$")

HELP = (
    "Puedo ayudarte con:\n"
    "• *pedidos* — resumen de pendientes y entregas programadas\n"
    "• *pedido 12* — detalle del PED-0012\n"
    "• *alta* Juan Pérez 871 123 4567 — registrar un cliente\n"
    "• *baja* 871 123 4567 — dar de baja a un cliente\n"
    "• *clientes* — lista de clientes activos\n"
    "• *buscar* juan — buscar un cliente\n"
    "También entiendo frases como «¿qué pedidos hay pendientes?». "
    "Las entregas se programan en el panel."
)


@dataclass(frozen=True)
class Command:
    action: str  # alta | baja | clientes | buscar | pedidos | pedido | ayuda | cortesia | solo_panel | no_entendido
    argument: str = ""


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


_THANKS = {"gracias", "ok", "okay", "listo", "perfecto", "va", "sale", "enterado", "excelente", "bien"}
_GREETINGS = {"hola", "buenas", "buenos", "buen", "hey", "ayuda", "help", "menu", "comandos", "opciones", "inicio"}
_ALTA = re.compile(
    r"^\s*(?:por\s+favor\s*,?\s*)?(?:(?:d[aá]|dar|dame|darle|haz|hacer)\s+(?:de\s+)?alta|alta|agreg(?:a|ar|ue)"
    r"|registr(?:a|ar|e)|nuevo\s+cliente)\b\s*:?\s*(?:(?:a|al)\s+)?(?:(?:el|la|un|una)\s+)?"
    r"(?:(?:nuevo\s+)?cliente\s*:?\s*)?",
    re.IGNORECASE,
)
_BAJA = re.compile(r"\b(?:(?:d[aá]|dar|darle)\s+de\s+baja|baja|desactiv(?:a|ar|e)|elimin(?:a|ar|e)|borr(?:a|ar|e))\b", re.I)
_PHONE_LABEL = re.compile(
    r"[\s,]+(?:y\s+)?(?:con\s+)?(?:el\s+)?(?:n[uú]mero|n[uú]m|tel[eé]fono|tel|cel(?:ular)?|whats(?:app)?)\s*:?\s*(?=\+?\(?\d)",
    re.IGNORECASE,
)
_FOLIO = re.compile(r"\b(?:ped(?:ido)?|folio|orden)\s*(?:no\.?|n[uú]mero)?\s*[-#]?\s*(\d{1,6})\b|#\s*(\d{1,6})\b", re.I)
_SEARCH = re.compile(
    r"\bb[uú]sca(?:r|me)?\b\s*(?:(?:a|al)\s+)?(?:(?:el|la)\s+)?(?:cliente\s+)?(?P<term>.+)", re.IGNORECASE
)
_ORDER_WORDS = ("pedido", "pendiente", "entrega", "programad", "orden")
# Acciones que solo se hacen en el panel.
_PANEL_ONLY = {"programar", "programa", "reprogramar", "agendar", "confirmar", "confirma", "entregado", "entregada",
               "entregar", "cancelar", "cancela", "cancelado"}
PANEL_ONLY_REPLY = (
    "Para programar entregas, marcar pedidos como entregados o cancelarlos usa el panel de administración "
    "(Pedidos → Gestionar). Por aquí puedes consultarlos: *pedidos* o *pedido 12*."
)


def parse_command(text: str | None) -> Command:
    """Interpreta un comando corto o una frase. Lo que no se entiende devuelve la ayuda."""
    raw = (text or "").strip()
    words = re.findall(r"[a-z0-9ñ]+", _fold(raw))
    if not words:
        return Command("ayuda")

    if _ALTA.match(raw) and not _BAJA.search(raw):
        return Command("alta", _ALTA.sub("", raw, count=1).strip(" .:"))
    if words[0] in _PANEL_ONLY:
        return Command("solo_panel")
    if baja := _BAJA.search(raw):
        phone = re.search(_PHONE, raw)
        rest = re.sub(r"^\s*(?:(?:a|al)\s+)?(?:(?:el|la)\s+)?(?:cliente\s*)?", "", raw[baja.end():], flags=re.I)
        return Command("baja", phone.group(0) if phone else rest.strip(" .:,"))
    if folio := _FOLIO.search(raw):
        return Command("pedido", str(int(folio.group(1) or folio.group(2))))
    if search := _SEARCH.search(raw):
        return Command("buscar", search.group("term").strip(" ?¿.!¡"))
    if any(w.startswith(_ORDER_WORDS) for w in words):
        return Command("pedidos")
    if any(w.startswith("cliente") for w in words):
        return Command("clientes")
    if words[0] in _GREETINGS:
        return Command("ayuda")
    if words[0] in _THANKS:
        return Command("cortesia")
    return Command("no_entendido", raw)


def parse_admin_phone(raw: str) -> str:
    """Como ``normalize_phone``, pero 10 dígitos se toman como número mexicano (+52)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "52" + digits
    return normalize_phone(digits)


def _split_name_phone(argument: str) -> tuple[str, str] | None:
    argument = _PHONE_LABEL.sub(" ", argument).strip(" .")  # «Juan con el teléfono 871…» -> «Juan 871…»
    for pattern in (_ALTA_NAME_FIRST, _ALTA_PHONE_FIRST):
        match = pattern.match(argument.strip())
        if match:
            name = match.group("name").strip(" ,.-")
            if name and not name.replace(" ", "").isdigit():
                return name, match.group("phone")
    return None


def _line(customer: Customer) -> str:
    status = "" if customer.active else " (inactivo)"
    return f"• {customer.name} — {format_phone_display(customer.phone)}{status}"


def _listing(title: str, customers: list[Customer], empty: str) -> str:
    if not customers:
        return empty
    lines, used = [], len(title)
    for i, customer in enumerate(customers):
        line = _line(customer)
        if used + len(line) + 1 > MAX_REPLY_CHARS:
            lines.append(f"… y {len(customers) - i} más. Usa *buscar* para filtrar.")
            break
        lines.append(line)
        used += len(line) + 1
    return title + "\n" + "\n".join(lines)


STATUS_LABELS = {
    OrderStatus.PENDING_SALES: "Pendiente de ventas",
    OrderStatus.SCHEDULED: "Entrega programada",
    OrderStatus.CLOSED: "Entregado",
    OrderStatus.CANCELLED: "Cancelado",
    OrderStatus.DRAFT: "Borrador",
    OrderStatus.AWAITING_CONFIRMATION: "Esperando confirmación del cliente",
}


def _order_lines(conn: sqlite3.Connection, order: Order) -> list[texts.SummaryLine]:
    return [
        texts.SummaryLine(repository.get_product(conn, i.product_id), i.quantity, Unit(i.unit))
        for i in repository.get_order_items(conn, order.id)
    ]


def order_detail(conn: sqlite3.Connection, order: Order) -> str:
    customer = repository.get_customer(conn, order.customer_id)
    lines = [f"*{order.folio}* — {STATUS_LABELS.get(order.status, order.status)}",
             f"Cliente: {customer.name} ({format_phone_display(customer.phone)})"]
    lines += [f"• {line.product.name}: {texts.format_quantity(line.quantity, line.unit)}" for line in _order_lines(conn, order)]
    if order.requested_date:
        lines.append(f"Fecha solicitada: {texts.format_date(order.requested_date)}")
    if order.urgency:
        label = {"normal": "Normal", "high": "Alta", "critical": "Crítica"}[order.urgency.value]
        lines.append(f"Urgencia: {label}" + (f" ({order.urgency_note})" if order.urgency_note else ""))
    if order.notes:
        lines.append(f"Observaciones: {order.notes}")
    if order.delivery_date:
        lines.append(f"Entrega: {texts.format_date(order.delivery_date)}")
    if order.sales_note:
        lines.append(f"Nota de ventas: {order.sales_note}")
    return "\n".join(lines)


def _order_command(conn: sqlite3.Connection, command: Command) -> str:
    reference = command.argument.split(" ", 1)[0]
    if not reference:
        return "Escribe: *pedido* folio — ej. pedido 12"
    try:
        return order_detail(conn, order_tracking.find_order(conn, reference))
    except order_tracking.OrderActionError as exc:
        return str(exc)


_URGENCY = {"high": "urgencia alta", "critical": "urgencia crítica"}
_SHORT_DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _summary_line(row: dict, date_field: str, prefix: str) -> str:
    when = date.fromisoformat(row[date_field]) if row[date_field] else None
    parts = [f"• *{row['folio']}* · {row['customer_name']}", row["items"] or "sin productos"]
    if when:
        parts.append(f"{prefix} {_SHORT_DAYS[when.weekday()]} {when.strftime('%d/%m')}")
    if row["urgency"] in _URGENCY:
        parts.append(_URGENCY[row["urgency"]])
    return " — ".join(parts)


def _open_orders(conn: sqlite3.Connection) -> str:
    rows = repository.list_orders_for_admin(conn, statuses=order_tracking.OPEN_STATUSES, limit=LIST_LIMIT)
    if not rows:
        return "No hay pedidos pendientes ni entregas programadas."
    pending = sorted((r for r in rows if r["status"] == "pending_sales"), key=lambda r: r["requested_date"] or "")
    scheduled = sorted((r for r in rows if r["status"] == "scheduled"), key=lambda r: r["delivery_date"] or "")
    out = []
    if pending:
        out.append(f"*Pendientes de programar ({len(pending)})*")
        out += [_summary_line(r, "requested_date", "para el") for r in pending]
    if scheduled:
        out.append(("\n" if pending else "") + f"*Entregas programadas ({len(scheduled)})*")
        out += [_summary_line(r, "delivery_date", "entrega") for r in scheduled]
    example = int((pending or scheduled)[0]["folio"].split("-")[-1])
    out.append(f"\nDetalle: *pedido {example}*. Las entregas se programan en el panel.")
    text = "\n".join(out)
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS].rsplit("\n", 1)[0] + "\n… (lista recortada; consulta el panel)"
    return text


def _short_date(value: str | None) -> str:
    return texts.format_date(date.fromisoformat(value)) if value else "—"


def execute(conn: sqlite3.Connection, command: Command, *, sales_phone: str | None) -> str:
    """Aplica el comando (dentro de la transacción del worker) y devuelve la respuesta."""
    if command.action == "pedido":
        return _order_command(conn, command)
    if command.action == "pedidos":
        return _open_orders(conn)
    if command.action == "cortesia":
        return "¡Con gusto! Escribe *pedidos* cuando quieras ver el resumen."
    if command.action == "solo_panel":
        return PANEL_ONLY_REPLY
    if command.action == "no_entendido":
        return f"No entendí «{command.argument[:80]}».\n\n{HELP}"
    return _customer_command(conn, command, sales_phone=sales_phone)


def _customer_command(conn: sqlite3.Connection, command: Command, *, sales_phone: str | None) -> str:
    if command.action == "alta":
        parsed = _split_name_phone(command.argument)
        if parsed is None:
            return "Para dar de alta escribe: *alta* Nombre Teléfono\nEjemplo: alta Juan Pérez 871 123 4567"
        name, raw_phone = parsed
        try:
            phone = parse_admin_phone(raw_phone)
        except InvalidPhoneError:
            return f"El teléfono «{raw_phone}» no es válido. Usa 10 dígitos o el formato +52 871 123 4567."
        if sales_phone and phone == sales_phone:
            return "Ese es el número de ventas; no se puede registrar como cliente."
        previous = repository.get_customer_by_phone(conn, phone, active_only=False)
        customer = repository.upsert_customer(conn, name=name, phone=phone, active=True)
        logger.info("Cliente %s %s desde WhatsApp", customer.id, "actualizado" if previous else "registrado")
        shown = f"{customer.name} — {format_phone_display(customer.phone)}"
        if previous is None:
            return f"Cliente registrado: {shown}. Ya puede hacer pedidos por este medio."
        if not previous.active:
            return f"Cliente reactivado: {shown}. Ya puede volver a hacer pedidos."
        return f"Cliente actualizado: {shown}."

    if command.action == "baja":
        if not command.argument:
            return "Para dar de baja escribe: *baja* Teléfono\nEjemplo: baja 871 123 4567"
        try:
            phone = parse_admin_phone(command.argument)
        except InvalidPhoneError:
            return f"El teléfono «{command.argument}» no es válido. Usa 10 dígitos o el formato +52 871 123 4567."
        customer = repository.set_customer_active(conn, phone, False)
        if customer is None:
            return f"No hay ningún cliente con el teléfono {format_phone_display(phone)}."
        logger.info("Cliente %s desactivado desde WhatsApp", customer.id)
        return f"Cliente dado de baja: {customer.name} — {format_phone_display(customer.phone)}. Ya no podrá hacer pedidos."

    if command.action == "clientes":
        customers = repository.list_customers(conn, active_only=True, limit=LIST_LIMIT)
        active, total = repository.count_customers(conn)
        return _listing(f"Clientes activos: {active} (de {total} registrados)", customers, "No hay clientes activos.")

    if command.action == "buscar":
        if not command.argument:
            return "Escribe qué buscar. Ejemplo: buscar juan"
        customers = repository.list_customers(conn, search=command.argument, limit=LIST_LIMIT)
        return _listing(
            f"Resultados para «{command.argument}»:", customers, f"No encontré clientes con «{command.argument}»."
        )

    return HELP

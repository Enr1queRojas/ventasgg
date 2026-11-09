"""Comandos del número de ventas (``SALES_PHONE``) para administrar clientes desde WhatsApp.

Se interpretan con reglas fijas (sin LLM) para que una alta o baja nunca dependa de
cómo el modelo entienda el mensaje:

    alta Juan Pérez 871 123 4567     registra (o reactiva y renombra) un cliente
    baja 871 123 4567                lo desactiva: deja de poder hacer pedidos
    clientes                         lista los clientes activos
    buscar juan                      busca por nombre o teléfono (incluye inactivos)
    ayuda                            muestra estos comandos
"""

import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from app.db import repository
from app.models import Customer
from app.phone import InvalidPhoneError, format_phone_display, normalize_phone

logger = logging.getLogger("sales_bot.admin")

# WhatsApp corta los textos de más de 4096 caracteres; se deja margen.
MAX_REPLY_CHARS = 3500
LIST_LIMIT = 200

# Un teléfono: "+52 871 123 4567", "8711234567", "(871) 123-45-67"... al menos 10 dígitos.
_PHONE = r"\+?\(?\d[\d\s().\-]{8,}\d"
_ALTA_NAME_FIRST = re.compile(rf"^(?P<name>.+?)[\s,]+(?P<phone>{_PHONE})$")
_ALTA_PHONE_FIRST = re.compile(rf"^(?P<phone>{_PHONE})[\s,]+(?P<name>.+)$")

HELP = (
    "Comandos para administrar clientes:\n"
    "• *alta* Nombre Teléfono — ej. alta Juan Pérez 871 123 4567\n"
    "• *baja* Teléfono — ej. baja 871 123 4567\n"
    "• *clientes* — lista los clientes activos\n"
    "• *buscar* texto — busca por nombre o teléfono\n"
    "• *ayuda* — muestra este mensaje\n\n"
    "Aquí también recibirás los avisos de pedidos nuevos."
)


@dataclass(frozen=True)
class Command:
    action: str  # alta | baja | clientes | buscar | ayuda
    argument: str = ""


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


_ACTIONS = {
    "alta": "alta", "agregar": "alta", "agrega": "alta", "registrar": "alta", "registra": "alta",
    "baja": "baja", "desactivar": "baja", "desactiva": "baja",
    "clientes": "clientes", "lista": "clientes", "listar": "clientes",
    "buscar": "buscar", "busca": "buscar",
    "ayuda": "ayuda", "help": "ayuda", "menu": "ayuda", "comandos": "ayuda",
}


def parse_command(text: str | None) -> Command:
    text = (text or "").strip()
    if not text:
        return Command("ayuda")
    first, _, rest = text.partition(" ")
    action = _ACTIONS.get(_fold(first).strip(".,:;!¡¿?"))
    return Command(action, rest.strip()) if action else Command("ayuda")


def parse_admin_phone(raw: str) -> str:
    """Como ``normalize_phone``, pero 10 dígitos se toman como número mexicano (+52)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "52" + digits
    return normalize_phone(digits)


def _split_name_phone(argument: str) -> tuple[str, str] | None:
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


def execute(conn: sqlite3.Connection, command: Command, *, sales_phone: str | None) -> str:
    """Aplica el comando (dentro de la transacción del worker) y devuelve la respuesta."""
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

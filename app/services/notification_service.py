"""Textos controlados por el backend (no generados por el LLM) para situaciones sensibles."""

import re
from dataclasses import dataclass
from datetime import date

from app.models import Customer, Order, Product, Unit, Urgency
from app.phone import format_phone_display

# Meta rechaza parámetros de plantilla con saltos de línea, tabuladores o más de 4 espacios seguidos.
_WHITESPACE = re.compile(r"\s+")
TEMPLATE_PARAM_MAX = 400  # el cuerpo completo de la plantilla no puede pasar de 1024 caracteres

DELIVERY_NOTICE = "El área de ventas te asignará y confirmará la fecha de entrega."

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
URGENCY_LABELS = {Urgency.NORMAL: "Normal", Urgency.HIGH: "Alta", Urgency.CRITICAL: "Crítica"}


@dataclass(frozen=True)
class SummaryLine:
    product: Product
    quantity: float
    unit: Unit


# --------------------------------------------------------------------------- formato


def format_quantity(quantity: float, unit: Unit) -> str:
    number = f"{quantity:g}" if quantity != int(quantity) else f"{int(quantity):,}"
    if unit is Unit.KG:
        return f"{number} kg"
    if unit is Unit.PIEZAS:
        return f"{number} {'pieza' if quantity == 1 else 'piezas'}"
    return f"{number} {'bulto' if quantity == 1 else 'bultos'}"


def format_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def _lines(lines: list[SummaryLine]) -> str:
    return "\n".join(f"• {line.product.name}: {format_quantity(line.quantity, line.unit)}" for line in lines)


def _urgency(order: Order) -> str:
    label = URGENCY_LABELS[order.urgency or Urgency.NORMAL]
    return f"{label} ({order.urgency_note})" if order.urgency_note else label


# --------------------------------------------------------------------------- cliente


def unknown_customer_message(sales_phone: str | None) -> str:
    contact = f"al *{format_phone_display(sales_phone)}*" if sales_phone else "directamente"
    return (
        "Hola. Este número todavía no está registrado como cliente. "
        f"Por favor, contacta al área de ventas {contact} para solicitar tu alta. "
        "Una vez registrado, podrás hacer pedidos por este medio."
    )


def greeting(customer_name: str) -> str:
    """Saludo fijo cuando el cliente saluda sin tener un pedido en curso."""
    return (
        f"Hola {customer_name}\n"
        "Por favor indica cuántos kilos de Maíz, Frijol (Bill-z, Peruano, Pinto, Saltillo) o papel necesitas."
    )


def unsupported_message_reply() -> str:
    return (
        "Por ahora solo puedo leer mensajes de texto. "
        "¿Me escribes tu pedido? Por ejemplo: «30 bultos de maíz blanco para el martes»."
    )


def provisional_customer_reply(customer: Customer) -> str:
    """Respuesta cuando el agente de OpenAI no está configurado."""
    return (
        f"Hola, {customer.name}. Recibí tu mensaje. "
        "Muy pronto podrás registrar tus pedidos por este medio."
    )


def order_summary(order: Order, lines: list[SummaryLine]) -> str:
    parts = ["Este es tu pedido:", _lines(lines), ""]
    if order.requested_date:
        parts.append(f"Fecha solicitada: {format_date(order.requested_date)}")
    parts.append(f"Urgencia: {_urgency(order)}")
    if order.notes:
        parts.append(f"Observaciones: {order.notes}")
    parts += ["", "¿Lo registro así? Responde *sí* para confirmar o dime qué quieres cambiar."]
    return "\n".join(parts)


def progress_ack(lines: list[SummaryLine]) -> str:
    return "Anotado:\n" + _lines(lines) if lines else ""


def order_registered(folio: str) -> str:
    return (
        f"Tu pedido *{folio}* quedó registrado y se envió a ventas para su revisión. "
        f"*{DELIVERY_NOTICE}*"
    )


def order_cancelled() -> str:
    return "Listo, cancelé el pedido en curso. Si necesitas algo más, aquí estoy."


def no_active_order() -> str:
    return "No tienes ningún pedido en curso. ¿Qué te gustaría pedir?"


# --------------------------------------------------------------------------- vendedor


def sales_notification(folio: str, customer: Customer, order: Order, lines: list[SummaryLine]) -> str:
    parts = [
        f"NUEVO PEDIDO - {folio}",
        "",
        f"Cliente: {customer.name}",
        f"Teléfono: {format_phone_display(customer.phone)}",
        "",
        "Productos:",
        _lines(lines),
        "",
        f"Fecha solicitada: {order.requested_date.strftime('%d/%m/%Y') if order.requested_date else '-'}",
        f"Urgencia: {_urgency(order)}",
    ]
    if order.notes:
        parts.append(f"Observaciones: {order.notes}")
    parts += [
        "", "Estado: PENDIENTE DE VENTAS", "", "La fecha de entrega aún no está confirmada.",
        "Gestiónalo en el panel de administración (Pedidos).",
    ]
    return "\n".join(parts)


def _template_param(value: str | None) -> str:
    text = _WHITESPACE.sub(" ", value or "").strip()
    if len(text) > TEMPLATE_PARAM_MAX:
        text = text[: TEMPLATE_PARAM_MAX - 1].rstrip() + "…"
    return text or "-"


def sales_notification_params(folio: str, customer: Customer, order: Order, lines: list[SummaryLine]) -> list[str]:
    """Valores {{1}}..{{7}} de la plantilla de aviso al vendedor (ver README, «Aviso al vendedor»).

    Orden: folio, cliente, teléfono, productos, fecha solicitada, urgencia, observaciones.
    """
    products = "; ".join(f"{line.product.name}: {format_quantity(line.quantity, line.unit)}" for line in lines)
    return [
        _template_param(value)
        for value in (
            folio,
            customer.name,
            format_phone_display(customer.phone),
            products,
            order.requested_date.strftime("%d/%m/%Y") if order.requested_date else None,
            _urgency(order),
            order.notes,
        )
    ]


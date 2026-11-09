"""Seguimiento de pedidos por ventas (WhatsApp y servicio)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db import repository
from app.models import MessageType, OrderStatus, Unit
from app.services import order_tracking
from app.services.admin_commands import parse_delivery_date
from app.workers.message_worker import MessageWorker

SALES_PHONE = "+52 33 1111 2222"
SALES_WA_ID = "5213311112222"
CUSTOMER_PHONE = "525512345678"


def local_today() -> date:
    return datetime.now(ZoneInfo("America/Mexico_City")).date()  # la zona del negocio (TZ)


class Sender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.templates: list[tuple[str, str, str, list | None]] = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return f"wamid.{len(self.sent)}"

    def send_template(self, to, name, language_code="es_MX", components=None):
        self.templates.append((to, name, language_code, components))
        return f"wamid.t{len(self.templates)}"


def registered_order(conn, customer, products, *, requested=None):
    order = repository.create_draft_order(conn, customer.id)
    repository.set_order_item(conn, order.id, products[0].id, 30, Unit.BULTOS)
    repository.update_order(
        conn, order.id, requested_date=(requested or local_today() + timedelta(days=3)).isoformat(),
        urgency="high", status=OrderStatus.AWAITING_CONFIRMATION.value,
    )
    folio = repository.register_order(conn, order.id)
    return repository.find_order_by_folio(conn, folio)


@pytest.fixture
def make_sales(conn, db_path):
    def build(**overrides):
        settings = Settings(
            _env_file=None, app_env="test", database_path=db_path, sales_phone=SALES_PHONE,
            worker_enabled=False, **overrides,
        )
        sender = Sender()
        worker = MessageWorker(settings, sender)
        counter = iter(range(1, 1000))

        def say(text):
            before = len(sender.sent)
            repository.insert_inbound_message(
                conn, whatsapp_message_id=f"s{next(counter)}", phone=SALES_WA_ID, text=text, message_type=MessageType.TEXT
            )
            worker.run_once()
            return sender.sent[before:]

        say.sender = sender
        return say

    return build


@pytest.fixture
def sales(make_sales):
    return make_sales()


# ------------------------------------------------------------------ fechas

TODAY = date(2026, 11, 9)  # lunes


@pytest.mark.parametrize(
    ("text", "expected", "rest"),
    [
        ("hoy", date(2026, 11, 9), ""),
        ("mañana en la tarde", date(2026, 11, 10), "en la tarde"),
        ("pasado mañana", date(2026, 11, 11), ""),
        ("viernes", date(2026, 11, 13), ""),
        ("el miércoles temprano", date(2026, 11, 11), "temprano"),
        ("lunes", date(2026, 11, 16), ""),  # el próximo, no hoy
        ("15/11", date(2026, 11, 15), ""),
        ("5/1", date(2027, 1, 5), ""),  # ya pasó este año -> el siguiente
        ("15/11/2026 bodega 2", date(2026, 11, 15), "bodega 2"),
        ("15-11-26", date(2026, 11, 15), ""),
        ("2026-12-01", date(2026, 12, 1), ""),
    ],
)
def test_parse_delivery_date(text, expected, rest):
    assert parse_delivery_date(text, TODAY) == (expected, rest)


@pytest.mark.parametrize("text", ["", "pronto", "31/02", "32/11"])
def test_parse_delivery_date_invalid(text):
    assert parse_delivery_date(text, TODAY) is None


@pytest.mark.parametrize(("ref", "folio"), [("PED-0012", "PED-0012"), ("ped 12", "PED-0012"), ("#12", "PED-0012"), ("12", "PED-0012"), ("0012", "PED-0012")])
def test_parse_folio(ref, folio):
    assert order_tracking.parse_folio(ref) == folio


# ------------------------------------------------------------------ WhatsApp


def test_programar_notifies_customer(conn, customer, products, sales):
    order = registered_order(conn, customer, products)
    replies = sales(f"programar {order.id} mañana llega en la tarde")
    delivery = local_today() + timedelta(days=1)
    assert replies[0][0] == SALES_WA_ID and "programado" in replies[0][1] and "Se avisó al cliente" in replies[0][1]
    to, text = replies[1]
    assert to == CUSTOMER_PHONE
    assert order.folio in text and delivery.strftime("%d/%m/%Y") in text and "Nota de ventas: llega en la tarde" in text
    updated = repository.get_order(conn, order.id)
    assert updated.status is OrderStatus.SCHEDULED and updated.delivery_date == delivery
    assert updated.sales_note == "llega en la tarde"

    # reprogramar
    replies = sales(f"confirmar PED-{order.id:04d} 2099-01-15")
    assert "reprogramado" in replies[0][1] and "se reprogramó" in replies[1][1]


def test_entregado_and_cannot_change_after(conn, customer, products, sales):
    order = registered_order(conn, customer, products)
    replies = sales(f"entregado {order.id}")
    assert "entregado" in replies[0][1] and "Gracias por tu compra" in replies[1][1]
    updated = repository.get_order(conn, order.id)
    assert updated.status is OrderStatus.CLOSED and updated.delivered_at is not None
    [reply] = sales(f"cancelar {order.id}")
    assert "ya fue entregado" in reply[1]


def test_cancelar_with_reason(conn, customer, products, sales):
    order = registered_order(conn, customer, products)
    replies = sales(f"cancelar {order.id} sin existencia de maíz")
    assert "cancelado" in replies[0][1]
    assert "Motivo: sin existencia de maíz" in replies[1][1] and "+52 33 1111 2222" in replies[1][1]
    updated = repository.get_order(conn, order.id)
    assert updated.status is OrderStatus.CANCELLED and updated.sales_note == "sin existencia de maíz"
    [reply] = sales(f"programar {order.id} mañana")
    assert "está cancelado" in reply[1]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [("programar", "Escribe: *programar*"), ("programar 999 mañana", "No encontré el pedido"),
     ("programar {id} pronto", "No entendí la fecha"), ("programar {id} 01/01/2020", "ya pasó"),
     ("entregado abc", "No encontré el pedido")],
)
def test_order_command_errors(conn, customer, products, sales, text, fragment):
    order = registered_order(conn, customer, products)
    [reply] = sales(text.format(id=order.id))
    assert fragment in reply[1]
    assert repository.get_order(conn, order.id).status is OrderStatus.PENDING_SALES


def test_pedidos_and_detail(conn, customer, products, sales):
    first = registered_order(conn, customer, products)
    sales(f"programar {first.id} mañana")
    other = repository.upsert_customer(conn, name="Ana López", phone="+52 871 765 4321")
    pending = registered_order(conn, other, products)
    [listing] = sales("pedidos")
    assert "Pendientes de ventas (1)" in listing[1] and pending.folio in listing[1] and "Ana López" in listing[1]
    assert "Entregas programadas (1)" in listing[1] and first.folio in listing[1]
    [detail] = sales(f"pedido {first.id}")
    assert "Entrega programada" in detail[1] and "Maíz blanco: 30 bultos" in detail[1]
    assert "Urgencia: Alta" in detail[1]


def test_customer_notice_uses_template_when_configured(conn, customer, products, make_sales):
    sales = make_sales(order_update_template="actualizacion_pedido")
    order = registered_order(conn, customer, products)
    replies = sales(f"entregado {order.id}")
    assert len(replies) == 1  # a ventas en texto; al cliente, plantilla
    [(to, name, language, [body])] = sales.sender.templates
    assert (to, name, language) == (CUSTOMER_PHONE, "actualizacion_pedido", "es_MX")
    params = [p["text"] for p in body["parameters"]]
    assert params == ["Cliente de prueba", order.folio, "pedido entregado, gracias por tu compra"]
    row = conn.execute("SELECT * FROM messages WHERE message_type = 'template'").fetchone()
    assert row["order_id"] == order.id and "entregado" in row["text"]

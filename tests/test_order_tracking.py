"""Seguimiento de pedidos por ventas (WhatsApp y servicio)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db import repository
from app.models import MessageType, OrderStatus, Unit
from app.services import order_tracking
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


@pytest.mark.parametrize(("ref", "folio"), [("PED-0012", "PED-0012"), ("ped 12", "PED-0012"), ("#12", "PED-0012"), ("12", "PED-0012"), ("0012", "PED-0012")])
def test_parse_folio(ref, folio):
    assert order_tracking.parse_folio(ref) == folio


# ------------------------------------------------------------------ WhatsApp (solo consulta)


def test_pedidos_and_detail(conn, customer, products, sales):
    first = registered_order(conn, customer, products)
    repository.transition_order(conn, first.id, from_statuses=["pending_sales"], status="scheduled",
                                delivery_date=(local_today() + timedelta(days=1)).isoformat())
    other = repository.upsert_customer(conn, name="Ana López", phone="+52 871 765 4321")
    pending = registered_order(conn, other, products)
    [listing] = sales("pedidos")
    assert "Pendientes de programar (1)" in listing[1] and pending.folio in listing[1] and "Ana López" in listing[1]
    assert "Entregas programadas (1)" in listing[1] and first.folio in listing[1]
    [detail] = sales(f"pedido {first.id}")
    assert "Entrega programada" in detail[1] and "Maíz blanco: 30 bultos" in detail[1]
    assert "Urgencia: Alta" in detail[1]
    [missing] = sales("pedido 999")
    assert "No encontré el pedido" in missing[1]


@pytest.mark.parametrize("text", ["programar {id} mañana", "entregado {id}", "cancelar {id} sin existencia"])
def test_sales_cannot_change_orders_by_whatsapp(conn, customer, products, sales, text):
    """Los cambios de pedido solo se hacen en el panel: por WhatsApp responde la ayuda y nada cambia."""
    order = registered_order(conn, customer, products)
    replies = sales(text.format(id=order.id))
    assert len(replies) == 1 and replies[0][0] == SALES_WA_ID  # nada al cliente
    assert "usa el panel de administración" in replies[0][1]
    assert repository.get_order(conn, order.id).status is OrderStatus.PENDING_SALES


def test_no_pedidos(sales):
    [reply] = sales("pedidos")
    assert "No hay pedidos pendientes" in reply[1]


# ------------------------------------------------------------------ servicio


def test_service_transitions_never_enqueue_messages(conn, customer, products):
    today = local_today()
    order = registered_order(conn, customer, products)
    result = order_tracking.schedule_delivery(conn, order, today + timedelta(days=2), "  por   la tarde ", today=today)
    assert result.order.status is OrderStatus.SCHEDULED and result.order.sales_note == "por la tarde"
    assert "programado" in result.summary
    again = order_tracking.schedule_delivery(conn, result.order, today + timedelta(days=3), None, today=today)
    assert "reprogramado" in again.summary
    done = order_tracking.mark_delivered(conn, again.order)
    assert done.order.status is OrderStatus.CLOSED and done.order.delivered_at
    with pytest.raises(order_tracking.OrderActionError, match="ya fue entregado"):
        order_tracking.cancel_order(conn, done.order, "x")
    with pytest.raises(order_tracking.OrderActionError, match="ya pasó"):
        order_tracking.schedule_delivery(conn, registered_order(conn, repository.upsert_customer(
            conn, name="B", phone="+52 871 111 2233"), products), today - timedelta(days=1), None, today=today)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0

"""Panel: gestión de pedidos (lista, detalle, programar, entregar, cancelar)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.api.admin import csrf_token
from app.config import Settings
from app.db import repository
from app.main import create_app
from app.models import OrderStatus, Unit

AUTH = ("admin", "clave")


def local_today() -> date:
    return datetime.now(ZoneInfo("America/Mexico_City")).date()


@pytest.fixture
def client(db_path):
    settings = Settings(
        _env_file=None, app_env="test", database_path=db_path, worker_enabled=False,
        sales_phone="+52 33 1111 2222", admin_password="clave",
    )
    with TestClient(create_app(settings)) as c:
        yield c


def registered(conn, customer, product, qty=30):
    order = repository.create_draft_order(conn, customer.id)
    repository.set_order_item(conn, order.id, product.id, qty, Unit.BULTOS)
    repository.update_order(
        conn, order.id, requested_date=(local_today() + timedelta(days=2)).isoformat(), urgency="high",
        urgency_note="me urge", status=OrderStatus.AWAITING_CONFIRMATION.value,
    )
    return repository.find_order_by_folio(conn, repository.register_order(conn, order.id))


def post(client, path, **data):
    return client.post(path, auth=AUTH, data={"csrf": csrf_token(client.app.state.settings), **data}, follow_redirects=True)


def outbound(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM messages WHERE direction = 'outbound' ORDER BY id")]


def test_home_and_list_default_pending(client, conn, customer, products):
    order = registered(conn, customer, products[0])
    other = repository.upsert_customer(conn, name="Ana López", phone="+52 871 765 4321")
    done = registered(conn, other, products[0], qty=5)
    repository.transition_order(conn, done.id, from_statuses=["pending_sales"], status="closed", delivered_at="x")

    assert client.get("/admin", auth=AUTH, follow_redirects=False).headers["location"] == "/admin/pedidos"
    text = client.get("/admin/pedidos", auth=AUTH).text
    assert order.folio in text and done.folio not in text
    assert "Pendientes <span class=\"count\">1</span>" in text and "Entregados <span class=\"count\">1</span>" in text
    assert "Maíz blanco: 30 bultos" in text and "Alta (me urge)" in text
    assert done.folio in client.get("/admin/pedidos?estado=entregados", auth=AUTH).text
    assert done.folio in client.get("/admin/pedidos?estado=todos&q=ana", auth=AUTH).text
    assert order.folio not in client.get("/admin/pedidos?estado=todos&q=ana", auth=AUTH).text


def test_detail_page(client, conn, customer, products):
    order = registered(conn, customer, products[0])
    text = client.get(f"/admin/pedidos/{order.id}", auth=AUTH).text
    assert order.folio in text and "Cliente de prueba" in text and "Programar entrega" in text
    assert client.get("/admin/pedidos/999", auth=AUTH).status_code == 404


def test_schedule_from_panel_notifies(client, conn, customer, products):
    order = registered(conn, customer, products[0])
    delivery = local_today() + timedelta(days=3)
    page = post(client, f"/admin/pedidos/{order.id}/programar", fecha=delivery.isoformat(), nota="por la tarde", avisar="1")
    assert "programado" in page.text and "Se envió el aviso al cliente" in page.text
    updated = repository.get_order(conn, order.id)
    assert updated.status is OrderStatus.SCHEDULED and updated.delivery_date == delivery
    [msg] = outbound(conn)
    assert msg["phone"] == customer.phone and msg["order_id"] == order.id and "por la tarde" in msg["text"]
    assert "Reprogramar entrega" in page.text and "Al cliente" in page.text  # mensaje visible en el detalle


def test_schedule_without_notice(client, conn, customer, products):
    order = registered(conn, customer, products[0])
    post(client, f"/admin/pedidos/{order.id}/programar", fecha=(local_today() + timedelta(days=1)).isoformat())
    assert repository.get_order(conn, order.id).status is OrderStatus.SCHEDULED
    assert outbound(conn) == []


@pytest.mark.parametrize("fecha", ["", "2020-01-01", "no-es-fecha"])
def test_schedule_invalid_date(client, conn, customer, products, fecha):
    order = registered(conn, customer, products[0])
    page = post(client, f"/admin/pedidos/{order.id}/programar", fecha=fecha, avisar="1")
    assert "flash err" in page.text
    assert repository.get_order(conn, order.id).status is OrderStatus.PENDING_SALES and outbound(conn) == []


def test_deliver_and_cancel(client, conn, customer, products):
    order = registered(conn, customer, products[0])
    page = post(client, f"/admin/pedidos/{order.id}/entregado", avisar="1")
    assert "entregado" in page.text and "ya está cerrado" in page.text
    assert repository.get_order(conn, order.id).status is OrderStatus.CLOSED
    page = post(client, f"/admin/pedidos/{order.id}/cancelar", motivo="x", avisar="1")
    assert "ya fue entregado" in page.text
    assert len(outbound(conn)) == 1

    other = repository.upsert_customer(conn, name="Ana", phone="+52 871 765 4321")
    second = registered(conn, other, products[0])
    post(client, f"/admin/pedidos/{second.id}/cancelar", motivo="sin existencia", avisar="1")
    cancelled = repository.get_order(conn, second.id)
    assert cancelled.status is OrderStatus.CANCELLED and cancelled.sales_note == "sin existencia"
    assert "Motivo: sin existencia" in outbound(conn)[-1]["text"]


def test_actions_require_csrf(client, conn, customer, products):
    order = registered(conn, customer, products[0])
    r = client.post(f"/admin/pedidos/{order.id}/entregado", auth=AUTH, data={"avisar": "1"})
    assert r.status_code == 403
    assert repository.get_order(conn, order.id).status is OrderStatus.PENDING_SALES

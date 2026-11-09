"""Panel de administración web (/admin)."""

import re

import pytest
from fastapi.testclient import TestClient

from app.api.admin import csrf_token
from app.config import Settings
from app.db import repository
from app.main import create_app
from app.models import Unit

AUTH = ("admin", "clave-secreta")


def make_client(db_path, **overrides) -> TestClient:
    settings = Settings(
        _env_file=None, app_env="test", database_path=db_path, worker_enabled=False,
        sales_phone="+52 33 1111 2222", admin_password="clave-secreta", **overrides,
    )
    return TestClient(create_app(settings))


@pytest.fixture
def client(db_path):
    with make_client(db_path) as c:
        yield c


def token(client) -> str:
    return csrf_token(client.app.state.settings)


def test_disabled_without_password(db_path):
    settings = Settings(_env_file=None, app_env="test", database_path=db_path, worker_enabled=False)
    with TestClient(create_app(settings)) as c:
        assert c.get("/admin/clientes", auth=AUTH).status_code == 404


@pytest.mark.parametrize("auth", [None, ("admin", "otra"), ("otro", "clave-secreta")])
def test_requires_credentials(client, auth):
    response = client.get("/admin/clientes", auth=auth)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_home_redirects_to_customers(client):
    response = client.get("/admin", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/clientes"


def test_customers_page_lists_and_escapes(client, conn):
    repository.upsert_customer(conn, name="<script>x</script> Pérez", phone="+52 871 123 4567")
    response = client.get("/admin/clientes", auth=AUTH)
    assert response.status_code == 200
    assert "&lt;script&gt;x&lt;/script&gt; Pérez" in response.text and "<script>x" not in response.text
    assert "+52 87 1123 4567" in response.text
    assert response.headers["X-Frame-Options"] == "DENY"
    assert re.search(r'name="csrf" value="[0-9a-f]{64}"', response.text)


def test_add_customer(client, conn):
    response = client.post(
        "/admin/clientes", auth=AUTH, follow_redirects=False,
        data={"csrf": token(client), "name": "Juan Pérez", "phone": "871 123 4567"},
    )
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    customer = repository.get_customer_by_phone(conn, "528711234567")
    assert customer.name == "Juan Pérez" and customer.active
    page = client.get(response.headers["location"], auth=AUTH)
    assert "Cliente registrado: Juan Pérez" in page.text


def test_post_without_valid_csrf_is_rejected(client, conn):
    for data in ({"name": "X", "phone": "8711234567"}, {"csrf": "0" * 64, "name": "X", "phone": "8711234567"}):
        assert client.post("/admin/clientes", auth=AUTH, data=data).status_code == 403
    assert repository.list_customers(conn) == []


@pytest.mark.parametrize(
    ("data", "error"),
    [({"name": "Juan", "phone": "123"}, "Teléfono inválido"), ({"name": " ", "phone": "8711234567"}, "Falta el nombre"),
     ({"name": "Ventas", "phone": "33 1111 2222"}, "número de ventas")],
)
def test_add_customer_validation(client, conn, data, error):
    response = client.post("/admin/clientes", auth=AUTH, data={"csrf": token(client), **data})
    assert response.status_code == 200 and error in response.text
    assert repository.list_customers(conn) == []


def test_toggle_customer(client, conn):
    customer = repository.upsert_customer(conn, name="Ana", phone="+52 871 765 4321")
    url = f"/admin/clientes/{customer.id}/estado"
    client.post(url, auth=AUTH, data={"csrf": token(client), "active": "0"})
    assert repository.get_customer_by_phone(conn, "528717654321") is None
    assert "Inactivo" in client.get("/admin/clientes?estado=inactivos", auth=AUTH).text
    client.post(url, auth=AUTH, data={"csrf": token(client), "active": "1"})
    assert repository.get_customer_by_phone(conn, "528717654321").active


def test_search_filter(client, conn):
    repository.upsert_customer(conn, name="Juan Pérez", phone="+52 871 123 4567")
    repository.upsert_customer(conn, name="Ana López", phone="+52 871 765 4321")
    text = client.get("/admin/clientes", params={"q": "lópez"}, auth=AUTH).text
    assert "Ana López" in text and "Juan Pérez" not in text
    text = client.get("/admin/clientes", params={"q": "1234567"}, auth=AUTH).text
    assert "Juan Pérez" in text and "Ana López" not in text


def test_products_and_orders_pages(client, conn, customer, products):
    assert "MAIZ-BLANCO" in client.get("/admin/productos", auth=AUTH).text
    order = repository.create_draft_order(conn, customer.id)
    repository.set_order_item(conn, order.id, products[0].id, 30, Unit.BULTOS)
    text = client.get("/admin/pedidos", auth=AUTH).text
    assert "Cliente de prueba" in text and "Maíz blanco: 30 bultos" in text and "Borrador" in text

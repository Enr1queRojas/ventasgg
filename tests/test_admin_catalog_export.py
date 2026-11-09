"""Panel: catálogo editable y exportación de pedidos a Excel."""

from datetime import date, datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api.admin import csrf_token
from app.config import Settings
from app.db import repository
from app.main import create_app
from app.models import OrderStatus, Unit

AUTH = ("admin", "clave")


@pytest.fixture
def client(db_path):
    settings = Settings(
        _env_file=None, app_env="test", database_path=db_path, worker_enabled=False, admin_password="clave",
    )
    with TestClient(create_app(settings)) as c:
        yield c


def post(client, path, data):
    payload = {"csrf": csrf_token(client.app.state.settings), **data}
    return client.post(path, auth=AUTH, data=payload, follow_redirects=True)


# ------------------------------------------------------------------ catálogo


def test_create_product(client, conn):
    page = post(client, "/admin/productos", {
        "sku": "frijol-negro", "name": " Frijol  negro ", "aliases": "negro, frijol negro|jamapa",
        "units": ["kg", "bultos"], "kg_per_bag": "50,5",
    })
    assert "Producto agregado: Frijol negro (FRIJOL-NEGRO)" in page.text
    product = repository.get_product_by_sku(conn, "FRIJOL-NEGRO")
    assert product.name == "Frijol negro" and product.aliases == ("negro", "frijol negro", "jamapa")
    assert set(product.allowed_units) == {Unit.KG, Unit.BULTOS} and product.kg_per_bag == 50.5 and product.active


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"sku": "MAIZ BLANCO", "name": "X", "units": "kg"}, "Código inválido"),
        ({"sku": "NUEVO", "name": "", "units": "kg"}, "Falta el nombre"),
        ({"sku": "NUEVO", "name": "X"}, "al menos una unidad"),
        ({"sku": "NUEVO", "name": "X", "units": "litros"}, "Unidad no válida"),
        ({"sku": "NUEVO", "name": "X", "units": "kg", "kg_per_bag": "-3"}, "mayor que 0"),
        ({"sku": "NUEVO", "name": "X", "units": "kg", "kg_per_bag": "abc"}, "inválido"),
        ({"sku": "MAIZ-BLANCO", "name": "Duplicado", "units": "kg"}, "Ya existe"),
    ],
)
def test_create_product_validation(client, conn, products, data, error):
    page = post(client, "/admin/productos", data)
    assert error in page.text
    assert repository.get_product_by_sku(conn, "NUEVO") is None
    assert repository.get_product_by_sku(conn, "MAIZ-BLANCO").name == "Maíz blanco"


def test_edit_and_deactivate_product(client, conn, products):
    product = products[0]
    assert "Guardar cambios" in client.get(f"/admin/productos/{product.id}", auth=AUTH).text
    post(client, f"/admin/productos/{product.id}", {
        "name": "Maíz blanco premium", "aliases": "blanco", "units": ["bultos"], "kg_per_bag": "40",
        "active": "0", "sku": "OTRO-CODIGO",  # el código no cambia aunque lo manden
    })
    updated = repository.get_product(conn, product.id)
    assert updated.sku == "MAIZ-BLANCO" and updated.name == "Maíz blanco premium"
    assert updated.allowed_units == (Unit.BULTOS,) and updated.kg_per_bag == 40 and not updated.active
    post(client, f"/admin/productos/{product.id}", {"name": "Maíz blanco", "units": ["kg"], "active": ["1", "0"]})
    assert repository.get_product(conn, product.id).active
    assert client.get("/admin/productos/999", auth=AUTH).status_code == 404


# ------------------------------------------------------------------ Excel


def _registered(conn, customer, product, *, qty, unit):
    today = datetime.now(ZoneInfo("America/Mexico_City")).date()
    order = repository.create_draft_order(conn, customer.id)
    repository.set_order_item(conn, order.id, product.id, qty, unit)
    repository.update_order(conn, order.id, requested_date=(today + timedelta(days=2)).isoformat(), urgency="high",
                            notes="bodega 2", status=OrderStatus.AWAITING_CONFIRMATION.value)
    return repository.find_order_by_folio(conn, repository.register_order(conn, order.id))


def test_export_xlsx(client, conn, customer, products):
    amarillo = products[1]  # 40 kg por bulto
    first = _registered(conn, customer, amarillo, qty=10, unit=Unit.BULTOS)
    other = repository.upsert_customer(conn, name="Ana López", phone="+52 871 765 4321")
    second = _registered(conn, other, products[0], qty=200, unit=Unit.KG)
    repository.transition_order(conn, second.id, from_statuses=["pending_sales"], status="scheduled",
                                delivery_date="2026-12-01", sales_note="tarde")

    response = client.get("/admin/pedidos/exportar.xlsx?estado=todos", auth=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="pedidos-todos-' in response.headers["content-disposition"]
    wb = load_workbook(BytesIO(response.content))
    assert wb.sheetnames == ["Pedidos", "Partidas"]
    orders = list(wb["Pedidos"].iter_rows(values_only=True))
    assert orders[0][:3] == ("Folio", "Registrado", "Cliente")
    by_folio = {row[0]: row for row in orders[1:]}
    assert by_folio[second.folio][9] == "Entrega programada" and by_folio[second.folio][10].date() == date(2026, 12, 1)
    assert by_folio[first.folio][3] == "+52 55 1234 5678" and by_folio[first.folio][8] == "bodega 2"
    assert isinstance(by_folio[first.folio][1], datetime)
    items = {row[0]: row for row in list(wb["Partidas"].iter_rows(values_only=True))[1:]}
    assert items[first.folio][4:7] == (10, "bultos", 400)  # convierte a kg con kg_per_bag
    assert items[second.folio][4:7] == (200, "kg", 200)

    only_scheduled = load_workbook(BytesIO(client.get("/admin/pedidos/exportar.xlsx?estado=programados", auth=AUTH).content))
    assert [r[0] for r in list(only_scheduled["Pedidos"].iter_rows(values_only=True))[1:]] == [second.folio]


def test_export_requires_auth(client):
    assert client.get("/admin/pedidos/exportar.xlsx").status_code == 401


def test_export_never_writes_formulas(client, conn, customer, products):
    """Texto del cliente que parece fórmula se exporta como texto literal (inyección de fórmulas)."""
    order = _registered(conn, customer, products[0], qty=1, unit=Unit.BULTOS)
    conn.execute("UPDATE orders SET notes = '=HYPERLINK(\"http://x\",\"y\")', urgency_note = '=1+1' WHERE id = ?", (order.id,))
    content = client.get("/admin/pedidos/exportar.xlsx?estado=todos", auth=AUTH).content
    sheet = load_workbook(BytesIO(content))["Pedidos"]
    header = [c.value for c in sheet[1]]
    row = sheet[2]
    for column in ("Observaciones", "Nota de urgencia"):
        cell = row[header.index(column)]
        assert cell.data_type == "s" and cell.value.startswith("=")
    assert row[header.index("Observaciones")].value == '=HYPERLINK("http://x","y")'

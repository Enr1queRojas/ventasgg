"""Etapa 2: esquema, restricciones y repositorio."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app.db import repository
from app.db.database import SCHEMA_VERSION, init_db, transaction
from app.models import MessageType, OrderStatus, Unit


def test_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_db_is_idempotent(conn, customer):
    init_db(conn)
    assert repository.get_customer(conn, customer.id) is not None


def test_customer_lookup_accepts_any_phone_format(conn, customer):
    assert repository.get_customer_by_phone(conn, "5215512345678") == customer
    assert repository.get_customer_by_phone(conn, "525512345678") == customer
    assert repository.get_customer_by_phone(conn, "+52 55 0000 0000") is None


def test_inactive_customer_not_found_by_default(conn, customer):
    repository.upsert_customer(conn, name=customer.name, phone=customer.phone, active=False)
    assert repository.get_customer_by_phone(conn, customer.phone) is None
    assert repository.get_customer_by_phone(conn, customer.phone, active_only=False) is not None


def test_upsert_customer_does_not_duplicate(conn, customer):
    repository.upsert_customer(conn, name="Nuevo nombre", phone="+52 1 55 1234 5678")
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    assert repository.get_customer(conn, customer.id).name == "Nuevo nombre"


def test_product_roundtrip(conn, products):
    p = repository.get_product_by_sku(conn, "maiz-amarillo")
    assert p.name == "Maíz amarillo"
    assert p.allowed_units == (Unit.BULTOS, Unit.KG)
    assert p.kg_per_bag == 40
    assert [x.sku for x in repository.list_products(conn)] == ["MAIZ-AMARILLO", "MAIZ-BLANCO", "SALVADO-TRIGO"]


def test_product_requires_units(conn):
    with pytest.raises(ValueError):
        repository.upsert_product(conn, sku="X", name="X", allowed_units=[])
    with pytest.raises(ValueError):
        repository.upsert_product(conn, sku="X", name="X", allowed_units=["litros"])


def test_one_active_order_per_customer(conn, customer):
    draft = repository.create_draft_order(conn, customer.id)
    assert repository.get_active_order(conn, customer.id) == draft
    with pytest.raises(sqlite3.IntegrityError):
        repository.create_draft_order(conn, customer.id)

    conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (draft.id,))
    assert repository.get_active_order(conn, customer.id) is None
    assert repository.create_draft_order(conn, customer.id).status is OrderStatus.DRAFT


def test_registered_order_requires_folio_and_data(conn, customer):
    draft = repository.create_draft_order(conn, customer.id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE orders SET status = 'pending_sales' WHERE id = ?", (draft.id,))
    conn.execute(
        """UPDATE orders SET status = 'pending_sales', folio = 'PED-0001', confirmed_at = '2026-11-06T00:00:00Z',
           requested_date = '2026-11-10', urgency = 'high' WHERE id = ?""",
        (draft.id,),
    )
    assert repository.get_order(conn, draft.id).status is OrderStatus.PENDING_SALES


def test_requested_date_must_be_iso(conn, customer):
    draft = repository.create_draft_order(conn, customer.id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE orders SET requested_date = '10/11/2026' WHERE id = ?", (draft.id,))


def test_order_item_constraints(conn, customer, products):
    order = repository.create_draft_order(conn, customer.id)
    insert = "INSERT INTO order_items (order_id, product_id, quantity, unit) VALUES (?, ?, ?, ?)"
    conn.execute(insert, (order.id, products[0].id, 30, "bultos"))
    for bad in [(order.id, products[0].id, 0, "kg"), (order.id, products[0].id, 5, "litros"), (order.id, 9999, 5, "kg")]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, bad)
    items = repository.get_order_items(conn, order.id)
    assert [(i.quantity, i.unit) for i in items] == [(30, Unit.BULTOS)]


def test_message_whatsapp_id_unique(conn):
    insert = "INSERT INTO messages (whatsapp_message_id, direction, phone, text) VALUES (?, 'inbound', '5215512345678', 'hola')"
    conn.execute(insert, ("wamid.1",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("wamid.1",))
    conn.execute(insert, (None,))
    conn.execute(insert, (None,))  # varios NULL permitidos


def test_transaction_rolls_back(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            repository.upsert_customer(conn, name="Temporal", phone="+52 33 1111 2222")
            raise RuntimeError("falla")
    assert repository.get_customer_by_phone(conn, "+52 33 1111 2222", active_only=False) is None


FIXTURES = Path(__file__).parent / "fixtures"


def _schema_v3() -> str:
    """Esquema tal como estaba en la versión 3 (antes del seguimiento de pedidos)."""
    return (FIXTURES / "schema_v3.sql").read_text(encoding="utf-8")


def _schema_without_template_payload() -> str:
    schema = _schema_v3()
    start = schema.index("    -- Plantilla de Meta a enviar")
    end = schema.index("\n", schema.index("template_payload     TEXT")) + 1
    return schema[:start] + schema[end:]


def test_migration_v1_to_v2_keeps_items_and_allows_piezas(db_path):
    """Una base creada con el esquema v1 (unidades kg|bultos) se migra al arrancar sin perder partidas."""
    from app.db.database import connect

    v1_schema = (
        _schema_without_template_payload()
        .replace("CHECK (unit IN ('kg', 'bultos', 'piezas'))", "CHECK (unit IN ('kg', 'bultos'))")
        .replace("PRAGMA user_version = 3;", "PRAGMA user_version = 1;")
    )
    assert "user_version = 1;" in v1_schema
    conn = connect(db_path)
    conn.executescript(v1_schema)
    c = repository.upsert_customer(conn, name="C", phone="+52 55 1234 5678")
    p = repository.upsert_product(conn, sku="CAJA", name="Caja", allowed_units=[Unit.KG])
    o = repository.create_draft_order(conn, c.id)
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit) VALUES (?, ?, 5, 'kg')", (o.id, p.id))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit) VALUES (?, ?, 5, 'piezas')", (o.id, p.id))

    init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert [(i.quantity, i.unit) for i in repository.get_order_items(conn, o.id)] == [(5, Unit.KG)]
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit) VALUES (?, ?, 3, 'piezas')", (o.id, p.id))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_migration_v2_to_v3_adds_template_payload(db_path):
    """Una base v2 (sin template_payload) conserva sus mensajes y admite plantillas tras migrar."""
    from app.db.database import connect

    v2_schema = _schema_without_template_payload().replace("PRAGMA user_version = 3;", "PRAGMA user_version = 2;")
    assert "template_payload" not in v2_schema and "user_version = 2;" in v2_schema
    conn = connect(db_path)
    conn.executescript(v2_schema)
    old_id = repository.insert_inbound_message(
        conn, whatsapp_message_id="wamid.v2", phone="5215512345678", text="hola", message_type=MessageType.TEXT
    )

    init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT text, template_payload FROM messages WHERE id = ?", (old_id,)).fetchone()[:] == ("hola", None)
    new_id = repository.enqueue_outbound(
        conn, phone="523311112222", text="aviso", message_type=MessageType.TEMPLATE,
        template_payload='{"name": "nuevo_pedido_ventas", "language": "es_MX", "body_params": []}',
    )
    assert conn.execute("SELECT message_type FROM messages WHERE id = ?", (new_id,)).fetchone()[0] == "template"
    with pytest.raises(sqlite3.IntegrityError):
        repository.enqueue_outbound(conn, phone="523311112222", text="x", template_payload="no es json")
    conn.close()


def test_migration_v3_to_v4_keeps_orders_items_and_messages(db_path):
    """La reconstrucción de orders (nuevo estado 'scheduled') no pierde pedidos, partidas ni mensajes."""
    from app.db.database import connect

    conn = connect(db_path)
    conn.executescript(_schema_v3())
    c = repository.upsert_customer(conn, name="C", phone="+52 55 1234 5678")
    p = repository.upsert_product(conn, sku="MAIZ", name="Maíz", allowed_units=[Unit.KG])
    conn.execute(
        "INSERT INTO orders (id, folio, customer_id, requested_date, urgency, status, confirmed_at) "
        "VALUES (7, 'PED-0007', ?, '2026-11-11', 'high', 'pending_sales', '2026-11-09T00:00:00Z')", (c.id,)
    )
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit) VALUES (7, ?, 100, 'kg')", (p.id,))
    msg = repository.enqueue_outbound(conn, phone="525512345678", text="aviso", order_id=7)

    init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    order = repository.get_order(conn, 7)
    assert order.folio == "PED-0007" and order.status is OrderStatus.PENDING_SALES and order.delivery_date is None
    assert [(i.quantity, i.unit) for i in repository.get_order_items(conn, 7)] == [(100, Unit.KG)]
    assert conn.execute("SELECT order_id FROM messages WHERE id = ?", (msg,)).fetchone()[0] == 7
    with pytest.raises(sqlite3.IntegrityError):  # programado exige fecha de entrega
        conn.execute("UPDATE orders SET status = 'scheduled' WHERE id = 7")
    conn.execute("UPDATE orders SET status = 'scheduled', delivery_date = ? WHERE id = 7", (date(2026, 11, 12).isoformat(),))
    conn.close()

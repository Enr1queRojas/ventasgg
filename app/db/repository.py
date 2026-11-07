"""Consultas y modificaciones SQL centralizadas.

Las funciones reciben una conexión abierta; quien llama decide el alcance de la
transacción (``with transaction(conn): ...``) cuando agrupa varias escrituras.
"""

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from app.models import (
    ACTIVE_ORDER_STATUSES,
    Customer,
    Message,
    MessageDirection,
    MessageStatus,
    MessageType,
    Order,
    OrderItem,
    Product,
    Unit,
)
from app.phone import normalize_phone


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- customers


def _customer(row: sqlite3.Row) -> Customer:
    return Customer(
        id=row["id"],
        name=row["name"],
        phone=row["phone"],
        active=bool(row["active"]),
        created_at=row["created_at"],
    )


def get_customer_by_phone(conn: sqlite3.Connection, phone: str, *, active_only: bool = True) -> Customer | None:
    """Busca por teléfono en cualquier formato aceptado (``+52 55...``, ``521...``, etc.)."""
    sql = "SELECT * FROM customers WHERE phone = ?"
    if active_only:
        sql += " AND active = 1"
    row = conn.execute(sql, (normalize_phone(phone),)).fetchone()
    return _customer(row) if row else None


def get_customer(conn: sqlite3.Connection, customer_id: int) -> Customer | None:
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return _customer(row) if row else None


def upsert_customer(conn: sqlite3.Connection, *, name: str, phone: str, active: bool = True) -> Customer:
    row = conn.execute(
        """
        INSERT INTO customers (name, phone, active) VALUES (?, ?, ?)
        ON CONFLICT (phone) DO UPDATE SET name = excluded.name, active = excluded.active
        RETURNING *
        """,
        (name.strip(), normalize_phone(phone), int(active)),
    ).fetchone()
    return _customer(row)


# --------------------------------------------------------------------------- products


def _product(row: sqlite3.Row) -> Product:
    return Product(
        id=row["id"],
        sku=row["sku"],
        name=row["name"],
        aliases=tuple(json.loads(row["aliases"])),
        allowed_units=tuple(Unit(u) for u in json.loads(row["allowed_units"])),
        kg_per_bag=row["kg_per_bag"],
        active=bool(row["active"]),
    )


def list_products(conn: sqlite3.Connection, *, active_only: bool = True) -> list[Product]:
    sql = "SELECT * FROM products"
    if active_only:
        sql += " WHERE active = 1"
    return [_product(r) for r in conn.execute(sql + " ORDER BY name")]


def get_product_by_sku(conn: sqlite3.Connection, sku: str) -> Product | None:
    row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku.strip().upper(),)).fetchone()
    return _product(row) if row else None


def get_product(conn: sqlite3.Connection, product_id: int) -> Product | None:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return _product(row) if row else None


def upsert_product(
    conn: sqlite3.Connection,
    *,
    sku: str,
    name: str,
    allowed_units: Iterable[Unit | str],
    aliases: Iterable[str] = (),
    kg_per_bag: float | None = None,
    active: bool = True,
) -> Product:
    units = sorted({Unit(u.strip().lower() if isinstance(u, str) else u) for u in allowed_units})
    if not units:
        raise ValueError(f"El producto {sku} necesita al menos una unidad permitida.")
    if kg_per_bag is not None and kg_per_bag <= 0:
        raise ValueError(f"kg_per_bag debe ser positivo en {sku}.")
    clean_aliases = list(dict.fromkeys(a.strip() for a in aliases if a.strip()))

    row = conn.execute(
        """
        INSERT INTO products (sku, name, aliases, allowed_units, kg_per_bag, active)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (sku) DO UPDATE SET
            name = excluded.name,
            aliases = excluded.aliases,
            allowed_units = excluded.allowed_units,
            kg_per_bag = excluded.kg_per_bag,
            active = excluded.active,
            updated_at = ?
        RETURNING *
        """,
        (
            sku.strip().upper(),
            name.strip(),
            json.dumps(clean_aliases, ensure_ascii=False),
            json.dumps([u.value for u in units]),
            kg_per_bag,
            int(active),
            utc_now(),
        ),
    ).fetchone()
    return _product(row)


# --------------------------------------------------------------------------- orders


def _order(row: sqlite3.Row) -> Order:
    return Order.model_validate(dict(row))


def _item(row: sqlite3.Row) -> OrderItem:
    return OrderItem.model_validate(dict(row))


def get_order(conn: sqlite3.Connection, order_id: int) -> Order | None:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _order(row) if row else None


def get_active_order(conn: sqlite3.Connection, customer_id: int) -> Order | None:
    """Pedido en captura (``draft`` o ``awaiting_confirmation``) del cliente, si existe."""
    placeholders = ",".join("?" * len(ACTIVE_ORDER_STATUSES))
    row = conn.execute(
        f"SELECT * FROM orders WHERE customer_id = ? AND status IN ({placeholders})",
        (customer_id, *ACTIVE_ORDER_STATUSES),
    ).fetchone()
    return _order(row) if row else None


def create_draft_order(conn: sqlite3.Connection, customer_id: int) -> Order:
    """Crea un borrador. Falla con ``sqlite3.IntegrityError`` si el cliente ya tiene uno activo."""
    row = conn.execute("INSERT INTO orders (customer_id) VALUES (?) RETURNING *", (customer_id,)).fetchone()
    return _order(row)


def get_order_items(conn: sqlite3.Connection, order_id: int) -> list[OrderItem]:
    return [_item(r) for r in conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))]


def set_order_item(conn: sqlite3.Connection, order_id: int, product_id: int, quantity: float, unit: Unit) -> None:
    """Una partida por producto: reemplaza la existente (cantidad y unidad)."""
    conn.execute("DELETE FROM order_items WHERE order_id = ? AND product_id = ?", (order_id, product_id))
    conn.execute(
        "INSERT INTO order_items (order_id, product_id, quantity, unit) VALUES (?, ?, ?, ?)",
        (order_id, product_id, quantity, unit),
    )


def remove_order_item(conn: sqlite3.Connection, order_id: int, product_id: int) -> int:
    return conn.execute(
        "DELETE FROM order_items WHERE order_id = ? AND product_id = ?", (order_id, product_id)
    ).rowcount


def clear_order_items(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))


_ORDER_EDITABLE_FIELDS = {"requested_date", "urgency", "urgency_note", "notes", "status"}


def update_order(conn: sqlite3.Connection, order_id: int, **fields: object) -> None:
    unknown = set(fields) - _ORDER_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Campos no editables: {unknown}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE orders SET {assignments}, updated_at = ? WHERE id = ?",
        (*[str(v) if v is not None else None for v in fields.values()], utc_now(), order_id),
    )


def register_order(conn: sqlite3.Connection, order_id: int) -> str | None:
    """Pasa un pedido de ``awaiting_confirmation`` a ``pending_sales`` y asigna folio.

    Devuelve el folio, o ``None`` si el pedido ya no estaba esperando confirmación
    (evita registrar dos veces).
    """
    folio = f"PED-{order_id:04d}"
    now = utc_now()
    updated = conn.execute(
        """
        UPDATE orders SET status = 'pending_sales', folio = ?, confirmed_at = ?, updated_at = ?,
               urgency = COALESCE(urgency, 'normal')
        WHERE id = ? AND status = 'awaiting_confirmation'
        """,
        (folio, now, now, order_id),
    ).rowcount
    return folio if updated else None


def insert_inbound_message(
    conn: sqlite3.Connection,
    *,
    whatsapp_message_id: str,
    phone: str,
    text: str | None,
    message_type: MessageType,
) -> int | None:
    """Encola un mensaje entrante como ``pending``. Devuelve su id, o ``None`` si el
    ``whatsapp_message_id`` ya existía (reintento/duplicado de Meta)."""
    row = conn.execute(
        """
        INSERT INTO messages (whatsapp_message_id, direction, phone, text, message_type, status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (whatsapp_message_id) DO NOTHING
        RETURNING id
        """,
        (whatsapp_message_id, MessageDirection.INBOUND, phone, text, message_type, MessageStatus.PENDING),
    ).fetchone()
    return row["id"] if row else None


def _message(row: sqlite3.Row) -> Message:
    return Message.model_validate(dict(row))


def claim_next_message(conn: sqlite3.Connection, direction: MessageDirection, now: str) -> Message | None:
    """Toma el siguiente mensaje ``pending`` listo y lo marca ``processing`` (sentencia atómica).

    Respeta el orden por teléfono: no toma un mensaje si hay otro anterior del mismo
    número aún pendiente (p. ej. esperando reintento) o en proceso.
    """
    row = conn.execute(
        """
        UPDATE messages SET status = 'processing', updated_at = :now
        WHERE id = (
            SELECT m.id FROM messages m
            WHERE m.direction = :direction AND m.status = 'pending'
              AND (m.next_attempt_at IS NULL OR m.next_attempt_at <= :now)
              AND NOT EXISTS (
                  SELECT 1 FROM messages p
                  WHERE p.direction = m.direction AND p.phone = m.phone AND p.id < m.id
                    AND p.status IN ('pending', 'processing')
              )
            ORDER BY m.id LIMIT 1
        )
        RETURNING *
        """,
        {"direction": direction, "now": now},
    ).fetchone()
    return _message(row) if row else None


def enqueue_outbound(
    conn: sqlite3.Connection,
    *,
    phone: str,
    text: str,
    message_type: MessageType = MessageType.TEXT,
    order_id: int | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO messages (direction, phone, text, message_type, status, order_id)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (MessageDirection.OUTBOUND, phone, text, message_type, MessageStatus.PENDING, order_id),
    ).fetchone()
    return row["id"]


def mark_processed(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute(
        "UPDATE messages SET status = 'processed', last_error = NULL, updated_at = ? WHERE id = ?",
        (utc_now(), message_id),
    )


def mark_sent(conn: sqlite3.Connection, message_id: int, whatsapp_message_id: str) -> None:
    conn.execute(
        """UPDATE messages SET status = 'sent', whatsapp_message_id = ?, attempts = attempts + 1,
           last_error = NULL, updated_at = ? WHERE id = ?""",
        (whatsapp_message_id, utc_now(), message_id),
    )


def schedule_retry(conn: sqlite3.Connection, message_id: int, *, attempts: int, next_attempt_at: str, error: str) -> None:
    conn.execute(
        """UPDATE messages SET status = 'pending', attempts = ?, next_attempt_at = ?, last_error = ?,
           updated_at = ? WHERE id = ?""",
        (attempts, next_attempt_at, error[:1000], utc_now(), message_id),
    )


def mark_failed(conn: sqlite3.Connection, message_id: int, *, attempts: int, error: str) -> None:
    conn.execute(
        "UPDATE messages SET status = 'failed', attempts = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (attempts, error[:1000], utc_now(), message_id),
    )


def recover_processing(conn: sqlite3.Connection) -> int:
    """Devuelve a ``pending`` los mensajes que quedaron en ``processing`` tras un reinicio."""
    return conn.execute(
        "UPDATE messages SET status = 'pending', updated_at = ? WHERE status = 'processing'", (utc_now(),)
    ).rowcount


def recent_messages(conn: sqlite3.Connection, phone: str, limit: int = 10) -> list[Message]:
    rows = conn.execute("SELECT * FROM messages WHERE phone = ? ORDER BY id DESC LIMIT ?", (phone, limit))
    return list(reversed([_message(r) for r in rows]))


def list_orders_by_status(conn: sqlite3.Connection, statuses: Sequence[str]) -> list[Order]:
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY created_at", tuple(statuses)
    )
    return [_order(r) for r in rows]

"""Conexiones SQLite, transacciones e inicialización del esquema.

Las conexiones se abren en modo autocommit (``isolation_level=None``) y las
escrituras se agrupan explícitamente con ``transaction()``, que usa
``BEGIN IMMEDIATE`` para tomar el bloqueo de escritura desde el inicio y
evitar errores de "database is locked" a mitad de la transacción.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 4

# Migraciones para bases existentes: versión de origen -> SQL que la lleva a la siguiente.
# SQLite no permite modificar un CHECK, así que se reconstruye la tabla.
MIGRATIONS: dict[int, str] = {
    1: """
    BEGIN;
    CREATE TABLE order_items_v2 (
        id          INTEGER PRIMARY KEY,
        order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        product_id  INTEGER NOT NULL REFERENCES products(id),
        quantity    REAL    NOT NULL CHECK (quantity > 0),
        unit        TEXT    NOT NULL CHECK (unit IN ('kg', 'bultos', 'piezas')),
        UNIQUE (order_id, product_id, unit)
    );
    INSERT INTO order_items_v2 (id, order_id, product_id, quantity, unit)
        SELECT id, order_id, product_id, quantity, unit FROM order_items;
    DROP TABLE order_items;
    ALTER TABLE order_items_v2 RENAME TO order_items;
    PRAGMA user_version = 2;
    COMMIT;
    """,
    2: """
    BEGIN;
    ALTER TABLE messages ADD COLUMN template_payload TEXT
        CHECK (template_payload IS NULL OR json_valid(template_payload));
    PRAGMA user_version = 3;
    COMMIT;
    """,
    # Seguimiento de pedidos: estado 'scheduled' y columnas de entrega. Cambia un CHECK, así que
    # se reconstruye la tabla con las llaves foráneas apagadas (si no, DROP borraría las partidas).
    3: """
    PRAGMA foreign_keys = OFF;
    BEGIN;
    CREATE TABLE orders_v4 (
        id              INTEGER PRIMARY KEY,
        folio           TEXT    UNIQUE,
        customer_id     INTEGER NOT NULL REFERENCES customers(id),
        requested_date  TEXT    CHECK (requested_date IS NULL OR date(requested_date) IS requested_date),
        urgency         TEXT    CHECK (urgency IS NULL OR urgency IN ('normal', 'high', 'critical')),
        urgency_note    TEXT,
        notes           TEXT,
        status          TEXT    NOT NULL DEFAULT 'draft'
                                CHECK (status IN ('draft', 'awaiting_confirmation', 'pending_sales',
                                                  'scheduled', 'cancelled', 'closed')),
        created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        confirmed_at    TEXT,
        delivery_date   TEXT    CHECK (delivery_date IS NULL OR date(delivery_date) IS delivery_date),
        sales_note      TEXT,
        delivered_at    TEXT,
        cancelled_at    TEXT,
        CHECK (status NOT IN ('pending_sales', 'scheduled', 'closed')
               OR (folio IS NOT NULL AND confirmed_at IS NOT NULL
                   AND requested_date IS NOT NULL AND urgency IS NOT NULL)),
        CHECK (status <> 'scheduled' OR delivery_date IS NOT NULL)
    );
    INSERT INTO orders_v4 (id, folio, customer_id, requested_date, urgency, urgency_note, notes, status,
                           created_at, updated_at, confirmed_at)
        SELECT id, folio, customer_id, requested_date, urgency, urgency_note, notes, status,
               created_at, updated_at, confirmed_at FROM orders;
    DROP TABLE orders;
    ALTER TABLE orders_v4 RENAME TO orders;
    PRAGMA user_version = 4;
    COMMIT;
    PRAGMA foreign_keys = ON;
    """,
}


def connect(db_path: Path | str) -> sqlite3.Connection:
    in_memory = str(db_path) == ":memory:"
    if not in_memory:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not in_memory:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def open_db(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Abre una conexión y la cierra al salir (``sqlite3.Connection`` no cierra por sí sola)."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection) -> None:
    """Crea tablas e índices si no existen y aplica las migraciones pendientes."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"La base es de una versión más nueva ({version}) que este código ({SCHEMA_VERSION})")
    if version > 0:  # base existente: migrar antes de ejecutar el esquema actual
        while version < SCHEMA_VERSION:
            conn.executescript(MIGRATIONS[version])
            version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("La base tiene referencias rotas (PRAGMA foreign_key_check)")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"Versión de esquema inesperada: {version} (se esperaba {SCHEMA_VERSION})")


def check_db(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1").fetchone()[0] == 1

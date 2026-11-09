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
SCHEMA_VERSION = 3

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
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"Versión de esquema inesperada: {version} (se esperaba {SCHEMA_VERSION})")


def check_db(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1").fetchone()[0] == 1

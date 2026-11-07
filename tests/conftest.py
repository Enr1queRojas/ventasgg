import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Settings
from app.db import repository
from app.db.database import connect, init_db
from app.models import Unit


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def settings(db_path: Path) -> Settings:
    # _env_file=None: las pruebas no dependen del .env local del desarrollador.
    return Settings(_env_file=None, app_env="test", database_path=db_path, worker_enabled=False)


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def customer(conn: sqlite3.Connection):
    return repository.upsert_customer(conn, name="Cliente de prueba", phone="+52 55 1234 5678")


@pytest.fixture
def products(conn: sqlite3.Connection):
    return [
        repository.upsert_product(
            conn, sku="MAIZ-BLANCO", name="Maíz blanco", aliases=["maiz bco", "blanco"],
            allowed_units=[Unit.KG, Unit.BULTOS],
        ),
        repository.upsert_product(
            conn, sku="MAIZ-AMARILLO", name="Maíz amarillo", aliases=["amarillo"],
            allowed_units=[Unit.KG, Unit.BULTOS], kg_per_bag=40,
        ),
        repository.upsert_product(
            conn, sku="SALVADO-TRIGO", name="Salvado de trigo", aliases=["salvado"],
            allowed_units=[Unit.BULTOS],
        ),
        repository.upsert_product(
            conn, sku="DESCONTINUADO", name="Producto descontinuado",
            allowed_units=[Unit.KG], active=False,
        ),
    ]

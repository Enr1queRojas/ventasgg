"""Carga o actualiza clientes y productos desde CSV (idempotente: upsert por teléfono / SKU).

Uso:  python -m scripts.seed_db [--db RUTA] [--customers CSV] [--products CSV]

Formato de los CSV (UTF-8; listas separadas por "|"):
  customers.csv: name,phone,active
  products.csv:  codigo,name,aliases,allowed_units,kg_per_bag,active   (codigo = SKU; también se acepta "sku")

Si una fila es inválida no se guarda nada y se indica la línea con el error.
"""

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import BASE_DIR, get_settings
from app.db import repository
from app.db.database import init_db, open_db, transaction
from app.phone import normalize_phone

SEED_DIR = BASE_DIR / "data" / "seed"
_TRUE = {"1", "true", "si", "sí", "yes", "y", "x"}
_FALSE = {"0", "false", "no", "n"}


class SeedError(ValueError):
    pass


@dataclass
class SeedResult:
    inserted: int = 0
    updated: int = 0


def _bool(value: str | None, *, default: bool = True) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return default
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"valor booleano inválido: '{value}'")


def _list(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def _rows(path: Path) -> list[tuple[int, dict[str, str]]]:
    # utf-8-sig tolera el BOM que agrega Excel al guardar CSV.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [(i, row) for i, row in enumerate(csv.DictReader(fh), start=2)]


def seed_customers(conn: sqlite3.Connection, path: Path) -> SeedResult:
    result = SeedResult()
    seen: set[str] = set()
    for line, row in _rows(path):
        try:
            name = (row.get("name") or "").strip()
            if not name:
                raise ValueError("falta el nombre")
            phone = normalize_phone(row.get("phone") or "")
            if phone in seen:
                raise ValueError(f"teléfono repetido en el archivo: {phone}")
            seen.add(phone)
            existed = repository.get_customer_by_phone(conn, phone, active_only=False) is not None
            repository.upsert_customer(conn, name=name, phone=phone, active=_bool(row.get("active")))
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise SeedError(f"{path.name}, línea {line}: {exc}") from exc
        if existed:
            result.updated += 1
        else:
            result.inserted += 1
    return result


def seed_products(conn: sqlite3.Connection, path: Path) -> SeedResult:
    result = SeedResult()
    seen: set[str] = set()
    for line, row in _rows(path):
        try:
            sku = (row.get("codigo") or row.get("sku") or "").strip().upper()
            if not sku:
                raise ValueError("falta el código del producto (columna codigo)")
            if sku in seen:
                raise ValueError(f"SKU repetido en el archivo: {sku}")
            seen.add(sku)
            kg_raw = (row.get("kg_per_bag") or "").strip()
            existed = repository.get_product_by_sku(conn, sku) is not None
            repository.upsert_product(
                conn,
                sku=sku,
                name=(row.get("name") or "").strip(),
                aliases=_list(row.get("aliases")),
                allowed_units=_list(row.get("allowed_units")),
                kg_per_bag=float(kg_raw) if kg_raw else None,
                active=_bool(row.get("active")),
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise SeedError(f"{path.name}, línea {line}: {exc}") from exc
        if existed:
            result.updated += 1
        else:
            result.inserted += 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, help="Ruta del archivo SQLite (por defecto DATABASE_PATH)")
    parser.add_argument("--customers", type=Path, default=SEED_DIR / "customers.csv")
    parser.add_argument("--products", type=Path, default=SEED_DIR / "products.csv")
    args = parser.parse_args()

    db_path = args.db or get_settings().database_path
    with open_db(db_path) as conn:
        init_db(conn)
        try:
            with transaction(conn):
                customers = seed_customers(conn, args.customers)
                products = seed_products(conn, args.products)
        except SeedError as exc:
            print(f"Error: {exc}. No se guardaron cambios.", file=sys.stderr)
            return 1

    print(f"Clientes: {customers.inserted} nuevos, {customers.updated} actualizados")
    print(f"Productos: {products.inserted} nuevos, {products.updated} actualizados")
    print(f"Base de datos: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

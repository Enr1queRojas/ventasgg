"""Crea tablas e índices en la base configurada.

Uso:  python -m scripts.init_db [--db RUTA]
"""

import argparse
from pathlib import Path

from app.config import get_settings
from app.db.database import init_db, open_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Ruta del archivo SQLite (por defecto DATABASE_PATH)")
    args = parser.parse_args()

    db_path = args.db or get_settings().database_path
    with open_db(db_path) as conn:
        init_db(conn)
    print(f"Base de datos lista: {db_path}")


if __name__ == "__main__":
    main()

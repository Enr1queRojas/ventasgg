"""Respaldo consistente de la base SQLite (API de backup de SQLite, seguro con el bot en marcha).

Uso:  python -m scripts.backup_db [--db RUTA] [--dir CARPETA] [--keep N]

Crea ``sales_bot-AAAAMMDD-HHMMSS.db`` en ``--dir`` (por defecto ``backups/`` junto a la
base, es decir ``/data/backups`` en Docker), comprueba su integridad y conserva solo los
``--keep`` respaldos más recientes. Imprime la ruta del respaldo creado.

Este respaldo vive en el mismo volumen que la base: para protegerse de perder la VM hay
que copiarlo fuera (``deploy/backup.sh`` lo copia al host; ver README).
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from app.config import get_settings

PREFIX = "sales_bot-"


def backup(db_path: Path, backup_dir: Path, *, keep: int, now: datetime | None = None) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"No existe la base {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{PREFIX}{stamp}.db"
    tmp = target.with_suffix(".tmp")

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"El respaldo no pasó la verificación de integridad: {result}")
    tmp.replace(target)

    for old in sorted(backup_dir.glob(f"{PREFIX}*.db"), reverse=True)[keep:]:
        old.unlink()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, help="Ruta de la base (por defecto DATABASE_PATH)")
    parser.add_argument("--dir", type=Path, help="Carpeta de respaldos (por defecto backups/ junto a la base)")
    parser.add_argument("--keep", type=int, default=14, help="Respaldos a conservar (por defecto 14)")
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep debe ser al menos 1")

    db_path = args.db or get_settings().database_path
    try:
        target = backup(db_path, args.dir or db_path.parent / "backups", keep=args.keep)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())

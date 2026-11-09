"""Respaldo de la base (scripts.backup_db)."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from app.db import repository
from scripts.backup_db import backup


def test_backup_copies_data_and_rotates(conn, customer, db_path, tmp_path):
    backup_dir = tmp_path / "backups"
    start = datetime(2026, 11, 9, 3, 0, 0)
    paths = [backup(db_path, backup_dir, keep=3, now=start + timedelta(days=i)) for i in range(5)]

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == [p.name for p in paths[-3:]]  # solo los 3 más recientes
    assert not list(backup_dir.glob("*.tmp"))
    copy = sqlite3.connect(paths[-1])
    copy.row_factory = sqlite3.Row
    assert repository.get_customer_by_phone(copy, customer.phone).name == customer.name
    copy.close()


def test_backup_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup(tmp_path / "no-existe.db", tmp_path / "b", keep=1)

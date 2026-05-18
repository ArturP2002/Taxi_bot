"""SQLite database backup for admins."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from peewee import SqliteDatabase

from app.config import get_settings
from app.db import PROJECT_ROOT, get_db


def sqlite_db_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite"):
        raise ValueError("backup_sqlite_only")
    raw = url[len("sqlite:///") :] if url.startswith("sqlite:///") else url[len("sqlite://") :]
    raw = raw.strip() or "taxi_bot.db"
    p = Path(raw)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def create_backup() -> Path:
    db = get_db()
    if not isinstance(db, SqliteDatabase):
        raise ValueError("backup_sqlite_only")

    src = sqlite_db_path()
    if not src.is_file():
        raise FileNotFoundError(str(src))

    backup_dir = PROJECT_ROOT / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"taxi_bot_{stamp}.db"

    if not db.is_closed():
        db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(src, dest)

    wal = Path(str(src) + "-wal")
    shm = Path(str(src) + "-shm")
    if wal.is_file():
        shutil.copy2(wal, backup_dir / f"{dest.stem}.db-wal")
    if shm.is_file():
        shutil.copy2(shm, backup_dir / f"{dest.stem}.db-shm")

    return dest

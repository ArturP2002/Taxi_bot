from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from peewee import Database, DatabaseProxy, SqliteDatabase
from playhouse.db_url import connect

from app.config import get_settings

_db: Database | None = None
db_proxy = DatabaseProxy()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_db() -> Database:
    global _db
    if _db is None:
        url = get_settings().database_url
        if url.startswith("sqlite"):
            raw = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url[len("sqlite://"):]
            raw = raw.strip()
            if not raw:
                raw = "taxi_bot.db"
            p = Path(raw)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            _db = SqliteDatabase(str(p), pragmas={
                'journal_mode': 'wal',
                'foreign_keys': 1,
                'busy_timeout': 5000,
            }, check_same_thread=False)
        else:
            _db = connect(url)
        db_proxy.initialize(_db)
    return _db


@contextmanager
def db_connection() -> Generator[Database, None, None]:
    db = get_db()
    db.connect(reuse_if_open=True)
    try:
        yield db
    finally:
        pass


def init_db() -> None:
    from app.models import ALL_MODELS

    db = get_db()
    db.connect(reuse_if_open=True)
    db.create_tables(ALL_MODELS, safe=True)


def ensure_connection() -> None:
    """Ensure DB connection is open."""
    db = get_db()
    if db.is_closed():
        db.connect(reuse_if_open=True)


def close_connection() -> None:
    """Close DB connection if not SQLite (SQLite keeps persistent connection)."""
    db = get_db()
    if not isinstance(db, SqliteDatabase) and not db.is_closed():
        db.close()


def reset_db_connection() -> None:
    """Close and drop cached DB handle (e.g. tests switching DATABASE_URL)."""
    global _db
    if _db is not None:
        try:
            if not _db.is_closed():
                _db.close()
        except Exception:
            pass
    _db = None

import pytest


@pytest.fixture(autouse=True)
def test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-at-least-32-bytes!!")
    monkeypatch.setenv("CODE_PEPPER", "test-pepper")
    monkeypatch.setenv("BOT_TOKEN", "0:test")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import init_db, reset_db_connection

    reset_db_connection()
    init_db()
    yield
    get_settings.cache_clear()
    reset_db_connection()

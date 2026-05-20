"""Integration smoke tests for FastAPI app — no bot, no Telegram."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1")
    monkeypatch.setenv("BOT_TOKEN", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    return TestClient(app)


def test_healthz_returns_db_path(monkeypatch):
    with _client(monkeypatch) as client:
        r = client.get("/healthz")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "ok"
        assert data["db_path"]
        assert data["admin_ids_configured"] == 1
        assert data["bot_token_configured"] is False
        assert data["users_total"] >= 0


def test_admin_endpoints_require_auth(monkeypatch):
    with _client(monkeypatch) as client:
        for path in ("/api/admin/drivers", "/api/admin/orders", "/api/admin/_diag"):
            r = client.get(path)
            assert r.status_code in (401, 403), f"{path}: {r.status_code} {r.text}"


def test_static_admin_served(monkeypatch):
    with _client(monkeypatch) as client:
        r = client.get("/admin/")
        assert r.status_code == 200
        assert "DaBro Taxi" in r.text or "DA BRO TAXI" in r.text

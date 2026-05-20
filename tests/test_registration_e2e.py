"""End-to-end driver registration without Telegram.

Reproduces the "анкета не упала в админку" scenario locally:
  1. ensure_user -> creates User + DriverProfile (pending).
  2. finalize_driver_registration -> persists profile, creates proposals.
  3. /api/admin/drivers must list the new driver as pending.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import (
    DriverProfile,
    DriverStatus,
    ProposedDirection,
    ProposedStatus,
    User,
)
from app.services import driver_registration as reg


class _FakeBot:
    def __init__(self):
        self.send_message = AsyncMock()
        self.send_media_group = AsyncMock()


def _make_pending_driver(tid: int = 999001) -> DriverProfile:
    u = User.create(telegram_id=tid, role="driver")
    return DriverProfile.create(user=u, status=DriverStatus.PENDING.value)


def test_finalize_registration_persists_and_creates_proposals(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1")
    from app.config import get_settings

    get_settings.cache_clear()

    bot = _FakeBot()
    d = _make_pending_driver()

    data = {
        "route_from": "Москва",
        "route_to": "Самара",
        "full_name": "Иван Тестов",
        "car_info": "Kia Rio, 2025, H567PK",
        "phone": "+79991234567",
        "max_seats": 8,
        "own_seats": 1,
        "price_per_seat": "90000",
        "fixed_price": "2000",
        "include_return": True,
    }

    ok, msg = asyncio.run(
        reg.finalize_driver_registration(bot, dprof=d, telegram_id=d.user.telegram_id, data=data)
    )
    assert ok, msg
    assert "Анкета отправлена" in msg

    saved = DriverProfile.get_by_id(d.id)
    assert saved.full_name == "Иван Тестов"
    assert saved.phone == "+79991234567"
    assert saved.car_info == "Kia Rio, 2025, H567PK"
    assert saved.status == DriverStatus.PENDING.value
    assert saved.max_seats == 8
    assert saved.own_seats_reserved == 1
    assert saved.proposed_price_per_seat == Decimal("90000")
    assert saved.proposed_fixed_price == Decimal("2000")

    props = list(
        ProposedDirection.select().where(ProposedDirection.proposer_id == saved.id)
    )
    assert len(props) >= 1, "forward proposal must exist"
    statuses = {p.status for p in props}
    assert statuses & {
        ProposedStatus.PENDING.value,
        ProposedStatus.RESERVED.value,
    }, statuses


def test_admin_api_lists_pending_driver(monkeypatch):
    """Anyone calling list_drivers() must see the new driver as pending+submitted."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1")
    from app.config import get_settings

    get_settings.cache_clear()

    bot = _FakeBot()
    d = _make_pending_driver(tid=999002)

    asyncio.run(
        reg.finalize_driver_registration(
            bot,
            dprof=d,
            telegram_id=d.user.telegram_id,
            data={
                "route_from": "Москва",
                "route_to": "Казань",
                "full_name": "Пётр Тестов",
                "car_info": "Lada Vesta",
                "phone": "+79990000001",
                "max_seats": 4,
                "own_seats": 0,
                "price_per_seat": "5000",
                "fixed_price": "1000",
                "include_return": False,
            },
        )
    )

    from app.api.admin_routes import list_drivers

    drivers = list_drivers()
    out = [d for d in drivers if d.telegram_id == 999002]
    assert out, "registered driver must appear in /api/admin/drivers"
    drv = out[0]
    assert drv.status == DriverStatus.PENDING.value
    assert drv.full_name == "Пётр Тестов"
    assert drv.phone == "+79990000001"
    assert drv.registration_submitted is True


def test_admin_diag_counts_growing(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1")
    from app.config import get_settings

    get_settings.cache_clear()

    from app.api.admin_routes import admin_diag

    before = admin_diag()
    assert "db_path" in before
    assert before["drivers_total"] >= 0

    bot = _FakeBot()
    d = _make_pending_driver(tid=999003)
    asyncio.run(
        reg.finalize_driver_registration(
            bot,
            dprof=d,
            telegram_id=d.user.telegram_id,
            data={
                "route_from": "А",
                "route_to": "Б",
                "full_name": "Тест",
                "car_info": "Авто",
                "phone": "+79990000002",
                "max_seats": 4,
                "own_seats": 0,
                "price_per_seat": "100",
                "fixed_price": "0",
                "include_return": True,
            },
        )
    )

    after = admin_diag()
    assert after["drivers_total"] == before["drivers_total"] + 1
    assert after["proposals_total"] >= before["proposals_total"] + 1


def test_grouped_proposals_includes_reserved(monkeypatch):
    """Резервные заявки должны быть в /proposals/grouped по умолчанию."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1")
    from app.config import get_settings

    get_settings.cache_clear()

    bot = _FakeBot()
    d = _make_pending_driver(tid=999004)
    asyncio.run(
        reg.finalize_driver_registration(
            bot,
            dprof=d,
            telegram_id=d.user.telegram_id,
            data={
                "route_from": "Москва",
                "route_to": "Уфа",
                "full_name": "Алексей Тест",
                "car_info": "Toyota Camry",
                "phone": "+79990000003",
                "max_seats": 4,
                "own_seats": 0,
                "price_per_seat": "100",
                "fixed_price": "0",
                "include_return": True,
            },
        )
    )

    from app.api.admin_routes import list_proposals_grouped

    groups = list_proposals_grouped()
    found = False
    for g in groups:
        for p in g.get("proposals", []):
            if p.get("proposer_id") == d.id:
                found = True
                break
    assert found, "proposal must appear in grouped list (pending or reserved)"

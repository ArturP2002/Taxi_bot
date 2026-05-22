from app.models import DriverProfile, User
from app.services import driver_registration as reg


def test_validate_single_city_rejects_route():
    ok, msg = reg.validate_single_city("Саратов — Сочи")
    assert not ok
    assert "один город" in msg.lower() or "маршрут" in msg.lower()


def test_validate_single_city_accepts_city():
    ok, city = reg.validate_single_city("Саратов")
    assert ok
    assert city == "Саратов"


def test_driver_needs_registration_pending_draft():
    u = User.create(telegram_id=900010, role="driver")
    d = DriverProfile.create(user=u, status="pending")
    assert reg.driver_needs_registration(d) is True


def test_driver_waiting_admin_after_submit():
    u = User.create(telegram_id=900011, role="driver")
    d = DriverProfile.create(
        user=u,
        status="pending",
        full_name="Иван",
        phone="+7999",
        car_info="Toyota",
    )
    assert reg.driver_needs_registration(d) is False
    assert reg.driver_waiting_admin(d) is True

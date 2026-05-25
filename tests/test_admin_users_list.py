from app.api.admin_routes import _passenger_user_ids
from app.models import Direction, Order, OrderStatus, User
from app.models.passenger import PassengerProfile
from app.models.user import UserRole


def test_passenger_user_ids_includes_driver_with_orders():
    u = User.create(telegram_id=990001, role=UserRole.DRIVER.value, username="drv_pax")
    d = Direction.create(from_label="A", to_label="B", estimated_time_min=60, min_time_percent=70)
    Order.create(
        direction=d,
        passenger=u,
        from_location="x",
        to_location="y",
        seats=1,
        phone="+7",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h",
    )
    assert u.id in _passenger_user_ids()


def test_passenger_user_ids_includes_profile_without_passenger_role():
    u = User.create(telegram_id=990002, role=UserRole.DRIVER.value)
    PassengerProfile.create(user=u)
    assert u.id in _passenger_user_ids()

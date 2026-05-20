from decimal import Decimal

from app.config import get_settings
from app.models import Direction, DriverProfile, DriverStatus, Order, OrderStatus, User, UserRole
from app.services import overflow_service
from app.services.debt_service import apply_debt_block_if_needed


def test_order_has_overflow_when_seats_exceed_capacity():
    d = Direction.create(from_label="A", to_label="B", estimated_time_min=60)
    pax = User.create(telegram_id=7001, role=UserRole.PASSENGER.value)
    Order.create(
        direction=d,
        passenger=pax,
        from_location="x",
        to_location="y",
        seats=5,
        platform_seats=5,
        phone="+1",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h",
    )
    order = Order.create(
        direction=d,
        passenger=pax,
        from_location="x2",
        to_location="y2",
        seats=5,
        platform_seats=5,
        phone="+2",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h2",
    )
    assert overflow_service.order_has_overflow(order) is True


def test_debt_block_at_threshold():
    u = User.create(telegram_id=8001, role=UserRole.DRIVER.value)
    drv = DriverProfile.create(
        user=u,
        balance=Decimal(str(get_settings().debt_block)),
        status=DriverStatus.ACTIVE.value,
    )
    assert apply_debt_block_if_needed(drv) is True
    drv = DriverProfile.get_by_id(drv.id)
    assert drv.status == DriverStatus.BLOCKED.value

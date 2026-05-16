from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.models import Direction, User, Order, DriverProfile, OrderStatus, OrderDriverAssignment, AssignmentStatus
from app.services import code_service, order_service, commission_service


def test_commission_percent():
    d = Direction.create(
        from_label="A",
        to_label="B",
        estimated_time_min=60,
        price_per_seat=Decimal("100"),
        fixed_price=Decimal("50"),
    )
    u = User.create(telegram_id=1, role="passenger")
    code = "111111"
    o = Order.create(
        direction=d,
        passenger=u,
        from_location="a",
        to_location="b",
        seats=2,
        phone="+1",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="tmp",
    )
    Order.update(confirmation_code_hash=code_service.hash_code(o.id, code)).where(Order.id == o.id).execute()
    o = Order.get_by_id(o.id)
    base = commission_service.order_base_fare(o)
    assert base == Decimal("250")
    assert commission_service.commission_amount_for_order(o) == Decimal("25")


def test_min_trip_and_complete(monkeypatch):
    d = Direction.create(
        from_label="A",
        to_label="B",
        estimated_time_min=100,
        min_time_percent=70,
        price_per_seat=Decimal("100"),
        fixed_price=Decimal("0"),
    )
    pu = User.create(telegram_id=10, role="passenger")
    du = User.create(telegram_id=20, role="driver")
    drv = DriverProfile.create(user=du, direction=d, max_seats=6, status="active", balance=Decimal("0"))
    code = "222222"
    o = Order.create(
        direction=d,
        passenger=pu,
        from_location="a",
        to_location="b",
        seats=1,
        phone="+1",
        status=OrderStatus.ASSIGNED.value,
        confirmation_code_hash="tmp",
    )
    Order.update(confirmation_code_hash=code_service.hash_code(o.id, code)).where(Order.id == o.id).execute()
    o = Order.get_by_id(o.id)
    OrderDriverAssignment.create(order=o, driver=drv, status=AssignmentStatus.ACCEPTED.value)
    ok, _ = order_service.verify_order_code(o, code)
    assert ok
    o = Order.get_by_id(o.id)
    assert o.status == OrderStatus.IN_PROGRESS.value

    # too early
    ok, key = order_service.complete_order(o, drv)
    assert not ok and key == "too_early"

    past = datetime.now(timezone.utc) - timedelta(minutes=120)
    Order.update(started_at=past).where(Order.id == o.id).execute()
    o = Order.get_by_id(o.id)
    ok, _ = order_service.complete_order(o, drv)
    assert ok
    o = Order.get_by_id(o.id)
    assert o.status == OrderStatus.COMPLETED.value
    drv = DriverProfile.get_by_id(drv.id)
    assert drv.balance > 0

from app.models import (
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    AssignmentStatus,
    User,
)
from app.services import order_service


def test_decline_suggested_on_direction_change():
    pu = User.create(telegram_id=900020, role="passenger")
    du = User.create(telegram_id=900021, role="driver")
    d_fwd = Direction.create(from_label="A", to_label="B", enabled=True, estimated_time_min=60)
    d_rev = Direction.create(from_label="B", to_label="A", enabled=True, estimated_time_min=60)
    drv = DriverProfile.create(
        user=du,
        status="active",
        direction_id=d_fwd.id,
        max_seats=6,
        online=True,
        full_name="Test",
    )
    order = Order.create(
        direction=d_rev,
        passenger=pu,
        from_location="x",
        to_location="y",
        seats=2,
        platform_seats=2,
        phone="1",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h",
    )
    ass = OrderDriverAssignment.create(
        order=order,
        driver=drv,
        status=AssignmentStatus.SUGGESTED.value,
    )
    order_service.decline_suggested_assignments(driver_id=drv.id)
    ass = OrderDriverAssignment.get_by_id(ass.id)
    assert ass.status == AssignmentStatus.DECLINED.value


def test_suggest_skips_wrong_direction():
    pu = User.create(telegram_id=900022, role="passenger")
    du = User.create(telegram_id=900023, role="driver")
    d_fwd = Direction.create(from_label="C", to_label="D", enabled=True, estimated_time_min=60)
    d_rev = Direction.create(from_label="D", to_label="C", enabled=True, estimated_time_min=60)
    drv = DriverProfile.create(
        user=du,
        status="active",
        direction_id=d_fwd.id,
        max_seats=6,
        online=True,
        full_name="Drv",
    )
    from app.services import queue_service

    queue_service.enqueue_driver_end(d_rev, drv)
    order = Order.create(
        direction=d_rev,
        passenger=pu,
        from_location="a",
        to_location="b",
        seats=1,
        platform_seats=1,
        phone="2",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h2",
    )
    suggestion = order_service.suggest_driver_for_order(order)
    if suggestion:
        assert suggestion.driver_id != drv.id or drv.direction_id == d_rev.id

from app.models import (
    AssignmentStatus,
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    User,
    UserRole,
)
from app.services import loading_service, order_service


def test_find_best_driver_respects_capacity():
    d = Direction.create(
        from_label="A",
        to_label="B",
        estimated_time_min=60,
        min_time_percent=70,
        enabled=True,
        price_per_seat=0,
        fixed_price=0,
        vehicle_capacity_default=4,
    )
    u = User.create(telegram_id=880100001, role=UserRole.DRIVER.value)
    drv = DriverProfile.create(user=u, status="active", online=True, max_seats=4, direction=d)
    pu = User.create(telegram_id=880100002, role=UserRole.PASSENGER.value)
    o1 = Order.create(
        direction=d,
        passenger=pu,
        from_location="x",
        to_location="y",
        seats=3,
        phone="1",
        status=OrderStatus.ASSIGNED.value,
        confirmation_code_hash="h",
    )
    OrderDriverAssignment.create(
        order=o1, driver=drv, status=AssignmentStatus.ACCEPTED.value
    )
    pu2 = User.create(telegram_id=880100003, role=UserRole.PASSENGER.value)
    o2 = Order.create(
        direction=d,
        passenger=pu2,
        from_location="a",
        to_location="b",
        seats=2,
        phone="2",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h2",
    )
    best = loading_service.find_best_driver_for_order(o2)
    assert best is None
    snap = loading_service.driver_loading_snapshot(drv)
    assert snap.free_seats == 1
    assert "свободно 1" in snap.status_label or snap.free_seats == 1

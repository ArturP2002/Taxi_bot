from decimal import Decimal

from app.models import (
    AssignmentStatus,
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    User,
)
from app.services import order_service


def _setup_driver_and_orders():
    d = Direction.create(
        from_label="X", to_label="Y", estimated_time_min=60, price_per_seat=Decimal("100")
    )
    pu1 = User.create(telegram_id=8001, role="passenger")
    pu2 = User.create(telegram_id=8002, role="passenger")
    du = User.create(telegram_id=8003, role="driver")
    drv = DriverProfile.create(
        user=du, direction=d, max_seats=6, status="active", online=True, full_name="Drv"
    )
    from app.services import code_service

    o1 = Order.create(
        direction=d,
        passenger=pu1,
        from_location="a",
        to_location="b",
        seats=2,
        phone="1",
        status=OrderStatus.ASSIGNED.value,
        confirmation_code_hash="h1",
        boarding_code="111111",
    )
    o2 = Order.create(
        direction=d,
        passenger=pu2,
        from_location="c",
        to_location="d",
        seats=3,
        phone="2",
        status=OrderStatus.ASSIGNED.value,
        confirmation_code_hash="h2",
        boarding_code="222222",
    )
    code_service.persist_boarding_code(o1.id, "111111")
    code_service.persist_boarding_code(o2.id, "222222")
    OrderDriverAssignment.create(order=o1, driver=drv, status=AssignmentStatus.ACCEPTED.value)
    OrderDriverAssignment.create(order=o2, driver=drv, status=AssignmentStatus.ACCEPTED.value)
    return drv, o1, o2


def test_board_one_keeps_loading():
    drv, o1, o2 = _setup_driver_and_orders()
    o1 = Order.get_by_id(o1.id)
    ok, key = order_service.verify_passenger_boarding(
        o1, "111111", driver_id=drv.id, expected_order_id=o1.id
    )
    assert ok and key == "boarded", key
    o1 = Order.get_by_id(o1.id)
    assert o1.status == OrderStatus.ASSIGNED.value
    summary = order_service.driver_boarding_summary(drv)
    assert summary["boarded_seats"] == 2
    assert len(summary["waiting_boarding"]) == 1
    assert summary["free_seats"] == 1


def test_depart_only_after_boarding():
    drv, o1, o2 = _setup_driver_and_orders()
    o1 = Order.get_by_id(o1.id)
    o2 = Order.get_by_id(o2.id)
    ok, key, _ = order_service.depart_driver_trip(drv)
    assert not ok and key == "no_boarded_passengers"

    ok_b, key_b = order_service.verify_passenger_boarding(
        o1, "111111", driver_id=drv.id, expected_order_id=o1.id
    )
    assert ok_b, key_b
    ok, key, info = order_service.depart_driver_trip(drv)
    assert ok, key
    assert len(info["departed_orders"]) == 1
    o1 = Order.get_by_id(o1.id)
    o2 = Order.get_by_id(o2.id)
    assert o1.status == OrderStatus.IN_PROGRESS.value
    assert o2.status == OrderStatus.ASSIGNED.value

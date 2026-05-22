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
from app.services import code_service, order_service


def test_find_order_by_six_digit_without_active_order_id():
    d = Direction.create(
        from_label="A", to_label="B", estimated_time_min=60, price_per_seat=Decimal("1")
    )
    pu = User.create(telegram_id=900100, role="passenger")
    du = User.create(telegram_id=900101, role="driver")
    drv = DriverProfile.create(user=du, direction=d, max_seats=6, status="active")
    code = "231997"
    o = Order.create(
        direction=d,
        passenger=pu,
        from_location="x",
        to_location="y",
        seats=2,
        phone="1",
        status=OrderStatus.ASSIGNED.value,
        confirmation_code_hash="tmp",
    )
    code_service.persist_boarding_code(o.id, code)
    OrderDriverAssignment.create(order=o, driver=drv, status=AssignmentStatus.ACCEPTED.value)

    assert code_service.parse_verification_raw(code) is None
    found = order_service.find_order_for_driver_boarding_code(drv.id, code)
    assert found is not None
    assert found.id == o.id

    ok, key = order_service.verify_passenger_boarding(found, code, driver_id=drv.id)
    assert ok and key == "boarded"

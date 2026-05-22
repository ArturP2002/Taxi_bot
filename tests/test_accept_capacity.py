"""Accepting a PENDING assignment must not double-count seats as full."""
from app.models import (
    AssignmentStatus,
    Direction,
    DriverProfile,
    DriverStatus,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    User,
    UserRole,
)
from app.services import order_service


def test_driver_can_accept_after_admin_assign_when_order_fits():
    d = Direction.create(from_label="Сочи", to_label="Москва", estimated_time_min=60)
    pax1 = User.create(telegram_id=9101, role=UserRole.PASSENGER.value)
    pax2 = User.create(telegram_id=9102, role=UserRole.PASSENGER.value)
    du = User.create(telegram_id=9103, role=UserRole.DRIVER.value)
    drv = DriverProfile.create(
        user=du,
        direction=d,
        max_seats=8,
        own_seats_reserved=0,
        status=DriverStatus.ACTIVE.value,
        online=True,
    )
    o1 = Order.create(
        direction=d,
        passenger=pax1,
        from_location="a",
        to_location="b",
        seats=3,
        phone="+1",
        status=OrderStatus.ASSIGNED.value,
        confirmation_code_hash="h1",
    )
    OrderDriverAssignment.create(
        order=o1, driver=drv, status=AssignmentStatus.ACCEPTED.value
    )
    o2 = Order.create(
        direction=d,
        passenger=pax2,
        from_location="c",
        to_location="d",
        seats=5,
        phone="+2",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h2",
    )
    ass = order_service.assign_order_to_driver(o2, drv)
    assert ass.status == AssignmentStatus.PENDING.value
    order_service.driver_respond(ass, accept=True)
    ass = OrderDriverAssignment.get_by_id(ass.id)
    assert ass.status == AssignmentStatus.ACCEPTED.value

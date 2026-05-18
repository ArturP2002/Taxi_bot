from datetime import datetime, timedelta, timezone

from app.models import (
    AssignmentStatus,
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    QueueEntry,
    User,
    UserRole,
)
from app.services import queue_eta_service


_tid = 880000001


def _mk_driver() -> DriverProfile:
    global _tid
    _tid += 1
    u = User.create(telegram_id=_tid, role=UserRole.DRIVER.value)
    return DriverProfile.create(user=u, status="active", online=True, max_seats=6)


def test_queue_eta_positions():
    d = Direction.create(
        from_label="Москва",
        to_label="Тбилиси",
        estimated_time_min=180,
        min_time_percent=70,
        enabled=True,
        price_per_seat=0,
        fixed_price=0,
        vehicle_capacity_default=6,
    )
    a = _mk_driver()
    b = _mk_driver()
    QueueEntry.create(direction=d, driver=a, position=1)
    QueueEntry.create(direction=d, driver=b, position=2)

    schedule = queue_eta_service.compute_queue_schedule(d.id)
    assert len(schedule) == 2
    assert schedule[0].driver_id == a.id
    assert schedule[0].is_now
    assert schedule[1].minutes_until >= 170


def test_queue_eta_blocked_by_trip():
    d = Direction.create(
        from_label="A",
        to_label="B",
        estimated_time_min=120,
        min_time_percent=70,
        enabled=True,
        price_per_seat=0,
        fixed_price=0,
        vehicle_capacity_default=6,
    )
    busy = _mk_driver()
    waiting = _mk_driver()
    QueueEntry.create(direction=d, driver=waiting, position=1)

    now = datetime.now(timezone.utc)
    pu = User.create(telegram_id=999001, role=UserRole.PASSENGER.value)
    order = Order.create(
        direction=d,
        passenger=pu,
        from_location="x",
        to_location="y",
        seats=1,
        phone="1",
        status=OrderStatus.IN_PROGRESS.value,
        confirmation_code_hash="h",
        started_at=now - timedelta(minutes=30),
    )
    OrderDriverAssignment.create(
        order=order, driver=busy, status=AssignmentStatus.ACCEPTED.value
    )

    schedule = queue_eta_service.compute_queue_schedule(d.id)
    assert len(schedule) == 1
    assert schedule[0].driver_id == waiting.id
    assert schedule[0].minutes_until >= 80

from datetime import datetime, timedelta, timezone

from app.models import Direction, Order, OrderStatus, User
from app.models.scheduled_trip import ScheduledTrip, ScheduledTripStatus
from app.services import scheduled_trip_service as sts


def _direction():
    return Direction.create(
        from_label="A",
        to_label="B",
        estimated_time_min=120,
        min_time_percent=70,
    )


def test_book_and_release_seats():
    d = _direction()
    dep = datetime.now(timezone.utc) + timedelta(days=3)
    trip = sts.create_trip(direction_id=d.id, departure_at=dep, seats_total=4)
    sts.book_seats(trip.id, 2)
    trip = ScheduledTrip.get_by_id(trip.id)
    assert trip.seats_booked == 2
    sts.release_seats(trip.id, 2)
    trip = ScheduledTrip.get_by_id(trip.id)
    assert trip.seats_booked == 0
    assert trip.status == ScheduledTripStatus.OPEN.value


def test_activate_due_orders():
    d = _direction()
    dep = datetime.now(timezone.utc) - timedelta(hours=1)
    trip = sts.create_trip(direction_id=d.id, departure_at=dep, seats_total=4)
    u = User.create(telegram_id=800001)
    order = Order.create(
        direction=d,
        passenger=u,
        from_location="x",
        to_location="y",
        seats=1,
        phone="+7999",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="x",
        scheduled_trip_id=trip.id,
        scheduled_activated=False,
    )
    n = sts.activate_due_orders()
    assert n >= 1
    order = Order.get_by_id(order.id)
    assert order.scheduled_activated is True

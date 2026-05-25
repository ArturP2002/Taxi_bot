from datetime import datetime, timedelta, timezone

import pytest

from app.models import Direction, Order, OrderStatus, User
from app.models.scheduled_trip import ScheduledTrip
from app.services import scheduled_trip_service as sts
from app.services import trip_request_service as trs


def _direction():
    return Direction.create(
        from_label="A",
        to_label="B",
        estimated_time_min=120,
        min_time_percent=70,
    )


def test_validate_requested_departure_past():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with pytest.raises(ValueError, match="departure_in_past"):
        trs.validate_requested_departure(past)


def test_is_order_in_live_queue_awaiting_trip():
    d = _direction()
    u = User.create(telegram_id=900001)
    order = Order.create(
        direction=d,
        passenger=u,
        from_location="x",
        to_location="y",
        seats=1,
        phone="+7",
        status=OrderStatus.AWAITING_SCHEDULED_TRIP.value,
        confirmation_code_hash="pending",
        requested_departure_at=datetime.now(timezone.utc) + timedelta(days=5),
    )
    assert sts.is_order_in_live_queue(order) is False


def test_fulfill_order_with_trip():
    d = _direction()
    dep = datetime.now(timezone.utc) + timedelta(days=4)
    trip = sts.create_trip(direction_id=d.id, departure_at=dep, seats_total=6)
    u = User.create(telegram_id=900002)
    order = Order.create(
        direction=d,
        passenger=u,
        from_location="from",
        to_location="to",
        seats=2,
        phone="+7999",
        status=OrderStatus.AWAITING_SCHEDULED_TRIP.value,
        confirmation_code_hash="pending",
        requested_departure_at=dep,
    )
    out = trs.fulfill_order_with_trip(order.id, trip)
    assert out.status == OrderStatus.NEW.value
    assert out.scheduled_trip_id == trip.id
    trip = ScheduledTrip.get_by_id(trip.id)
    assert trip.seats_booked == 2


def test_fulfill_wrong_status():
    d = _direction()
    dep = datetime.now(timezone.utc) + timedelta(days=4)
    trip = sts.create_trip(direction_id=d.id, departure_at=dep, seats_total=6)
    u = User.create(telegram_id=900003)
    order = Order.create(
        direction=d,
        passenger=u,
        from_location="x",
        to_location="y",
        seats=1,
        phone="+7",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="x",
    )
    with pytest.raises(ValueError, match="order_not_awaiting_trip"):
        trs.fulfill_order_with_trip(order.id, trip)

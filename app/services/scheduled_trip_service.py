"""Scheduled trips: future departures with seat inventory."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Set

from app.config import get_settings
from app.models import Direction, DriverProfile, Order, OrderStatus
from app.models.scheduled_trip import (
    ScheduledTrip,
    ScheduledTripCreatedBy,
    ScheduledTripStatus,
)
from app.util.datetimeutil import utcnow


def _now() -> datetime:
    return utcnow()


def create_trip(
    *,
    direction_id: int,
    departure_at: datetime,
    seats_total: int,
    driver_id: Optional[int] = None,
    created_by: str = ScheduledTripCreatedBy.ADMIN.value,
    note: Optional[str] = None,
    status: Optional[str] = None,
) -> ScheduledTrip:
    if seats_total < 1:
        raise ValueError("seats_total must be >= 1")
    st = status or ScheduledTripStatus.OPEN.value
    return ScheduledTrip.create(
        direction_id=direction_id,
        departure_at=departure_at,
        seats_total=seats_total,
        seats_booked=0,
        status=st,
        driver_id=driver_id,
        created_by=created_by,
        note=note,
    )


def seats_available(trip: ScheduledTrip) -> int:
    return max(0, int(trip.seats_total) - int(trip.seats_booked or 0))


def list_open_by_direction(direction_id: int) -> list[ScheduledTrip]:
    now = _now()
    settings = get_settings()
    horizon = now + timedelta(days=settings.scheduled_trip_booking_days_ahead)
    q = (
        ScheduledTrip.select()
        .where(
            (ScheduledTrip.direction_id == direction_id)
            & (ScheduledTrip.status == ScheduledTripStatus.OPEN.value)
            & (ScheduledTrip.departure_at >= now)
            & (ScheduledTrip.departure_at <= horizon)
        )
        .order_by(ScheduledTrip.departure_at)
    )
    return [t for t in q if seats_available(t) > 0]


def available_dates_for_direction(direction_id: int) -> Set[date]:
    dates: Set[date] = set()
    for t in list_open_by_direction(direction_id):
        dep = t.departure_at
        if dep.tzinfo is None:
            dep = dep.replace(tzinfo=timezone.utc)
        dates.add(dep.date())
    return dates


def trips_on_date(direction_id: int, day: date) -> list[ScheduledTrip]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return [
        t
        for t in list_open_by_direction(direction_id)
        if start <= (t.departure_at if t.departure_at.tzinfo else t.departure_at.replace(tzinfo=timezone.utc)) < end
    ]


def book_seats(trip_id: int, seats: int) -> ScheduledTrip:
    trip = ScheduledTrip.get_by_id(trip_id)
    if trip.status != ScheduledTripStatus.OPEN.value:
        raise ValueError("trip_not_open")
    free = seats_available(trip)
    if seats > free:
        raise ValueError("not_enough_seats")
    new_booked = int(trip.seats_booked or 0) + seats
    trip.seats_booked = new_booked
    if new_booked >= int(trip.seats_total):
        trip.status = ScheduledTripStatus.FULL.value
    trip.updated_at = _now()
    trip.save()
    return trip


def release_seats(trip_id: int, seats: int) -> None:
    if not trip_id or seats <= 0:
        return
    try:
        trip = ScheduledTrip.get_by_id(trip_id)
    except Exception:
        return
    new_booked = max(0, int(trip.seats_booked or 0) - seats)
    trip.seats_booked = new_booked
    if trip.status == ScheduledTripStatus.FULL.value and new_booked < int(trip.seats_total):
        trip.status = ScheduledTripStatus.OPEN.value
    trip.updated_at = _now()
    trip.save()


def is_order_in_live_queue(order: Order) -> bool:
    """Orders on future scheduled trips stay out of live queue until activated."""
    if not order.scheduled_trip_id:
        return True
    if getattr(order, "scheduled_activated", False):
        return True
    return False


def trip_departure_day_reached(trip: ScheduledTrip) -> bool:
    now = _now()
    dep = trip.departure_at
    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=timezone.utc)
    return now.date() >= dep.date()


def order_should_activate_now(order: Order, trip: ScheduledTrip) -> bool:
    return trip_departure_day_reached(trip)


def activate_due_orders() -> int:
    """Activate scheduled orders whose departure day has arrived."""
    activated = 0
    orders = Order.select().where(
        (Order.scheduled_trip_id.is_null(False))
        & (Order.scheduled_activated == False)
        & (Order.status.in_([OrderStatus.NEW.value, OrderStatus.AWAITING_PAYMENT.value]))
    )
    for o in orders:
        try:
            trip = ScheduledTrip.get_by_id(o.scheduled_trip_id)
        except Exception:
            continue
        if order_should_activate_now(o, trip):
            Order.update(scheduled_activated=True).where(Order.id == o.id).execute()
            activated += 1
    return activated


def list_driver_trips(driver: DriverProfile, *, limit: int = 20) -> list[ScheduledTrip]:
    now = _now()
    q = (
        ScheduledTrip.select()
        .where(
            (ScheduledTrip.driver_id == driver.id)
            & (ScheduledTrip.departure_at >= now)
            & (ScheduledTrip.status.in_([
                ScheduledTripStatus.OPEN.value,
                ScheduledTripStatus.FULL.value,
                ScheduledTripStatus.DRAFT.value,
            ]))
        )
        .order_by(ScheduledTrip.departure_at)
        .limit(limit)
    )
    return list(q)

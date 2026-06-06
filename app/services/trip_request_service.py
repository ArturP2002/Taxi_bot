"""Passenger trip departure requests awaiting admin-scheduled trip."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot

from app.config import get_settings
from app.models import Direction, Order, OrderStatus, PassengerPaymentStatus, User
from app.models.scheduled_trip import ScheduledTrip
from app.services import audit_service, scheduled_trip_service
from app.util.datetimeutil import utcnow
from app.util.time_format import format_datetime_display


def list_pending_requests() -> list[Order]:
    return list(
        Order.select()
        .where(Order.status == OrderStatus.AWAITING_SCHEDULED_TRIP.value)
        .order_by(Order.requested_departure_at, Order.id)
    )


def validate_requested_departure(dep: datetime) -> None:
    now = utcnow()
    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=timezone.utc)
    if dep < now:
        raise ValueError("departure_in_past")
    horizon = now + timedelta(days=get_settings().scheduled_trip_booking_days_ahead)
    if dep > horizon:
        raise ValueError("departure_too_far")


def _target_order_status(direction: Direction) -> str:
    if getattr(direction, "online_payment_required", False):
        return OrderStatus.AWAITING_PAYMENT.value
    return OrderStatus.NEW.value


def find_best_trip_for_request(
    *,
    direction_id: int,
    requested_departure_at: datetime,
    seats: int,
) -> Optional[ScheduledTrip]:
    """Find nearest open trip with enough seats for requested datetime."""
    req = requested_departure_at
    if req.tzinfo is None:
        req = req.replace(tzinfo=timezone.utc)
    candidates = scheduled_trip_service.list_open_by_direction(direction_id)
    if not candidates:
        return None
    best: Optional[ScheduledTrip] = None
    best_delta: Optional[float] = None
    for trip in candidates:
        if scheduled_trip_service.seats_available(trip) < int(seats):
            continue
        dep = trip.departure_at
        if dep.tzinfo is None:
            dep = dep.replace(tzinfo=timezone.utc)
        delta = abs((dep - req).total_seconds())
        if best is None or best_delta is None or delta < best_delta:
            best = trip
            best_delta = delta
    return best


def fulfill_order_with_trip(order_id: int, trip: ScheduledTrip) -> Order:
    """Link awaiting order to trip, book seats, set status for dispatch."""
    order = Order.get_by_id(order_id)
    if order.status != OrderStatus.AWAITING_SCHEDULED_TRIP.value:
        raise ValueError("order_not_awaiting_trip")
    if order.direction_id != trip.direction_id:
        raise ValueError("direction_mismatch")
    scheduled_trip_service.book_seats(trip.id, int(order.seats))
    trip = ScheduledTrip.get_by_id(trip.id)
    direction = Direction.get_by_id(order.direction_id)
    activated = scheduled_trip_service.trip_departure_day_reached(trip)
    now = utcnow()
    Order.update(
        scheduled_trip_id=trip.id,
        scheduled_activated=activated,
        status=_target_order_status(direction),
        updated_at=now,
    ).where(Order.id == order_id).execute()
    return Order.get_by_id(order_id)


async def notify_passenger_trip_confirmed(
    bot: Bot, order: Order, trip: ScheduledTrip, *, code: str | None = None
) -> None:
    """Trip linked in calendar — билет с QR придёт после назначения водителя оператором."""
    direction = Direction.get_by_id(order.direction_id)
    dep_label = format_datetime_display(trip.departure_at)
    text = (
        f"✅ Рейс подтверждён · заказ #{order.id}\n"
        f"📍 {direction.from_label} → {direction.to_label}\n"
        f"📅 Выезд: {dep_label}\n\n"
        "Билет с QR-кодом, данными водителя и правилами придёт "
        "после назначения водителя оператором."
    )
    try:
        await bot.send_message(order.passenger.telegram_id, text)
    except Exception:
        pass


async def fulfill_and_notify(
    bot: Bot,
    order_id: int,
    trip: ScheduledTrip,
    *,
    actor_telegram_id: Optional[int] = None,
) -> Order:
    order = fulfill_order_with_trip(order_id, trip)
    order = Order.get_by_id(order.id)
    await notify_passenger_trip_confirmed(bot, order, trip)
    audit_service.log_action(
        "trip_request_fulfilled",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order_id),
        payload={"scheduled_trip_id": trip.id},
    )
    if order.status == OrderStatus.NEW.value:
        from app.services import order_service
        from app.services.admin_notify import notify_new_order

        direction = Direction.get_by_id(order.direction_id)
        if scheduled_trip_service.is_order_in_live_queue(order):
            suggestion = order_service.suggest_driver_for_order(order)
            suggested_name = None
            assignment_id = None
            if suggestion:
                from app.models import DriverProfile

                drv = DriverProfile.get_by_id(suggestion.driver_id)
                suggested_name = drv.full_name or f"ID:{drv.id}"
                assignment_id = suggestion.id
            await notify_new_order(
                bot,
                order.id,
                direction.from_label,
                direction.to_label,
                order.from_location,
                order.to_location,
                order.seats,
                suggested_driver_name=suggested_name,
                assignment_id=assignment_id,
            )
    return order


def user_display_name(user: User) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return f"ID {user.telegram_id}"

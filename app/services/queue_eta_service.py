"""Estimated loading time per driver in direction queue."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.config import get_settings
from app.models import (
    AssignmentStatus,
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    QueueEntry,
)
from app.util.time_format import minutes_to_hours_label


@dataclass
class QueueEtaSlot:
    driver_id: int
    position: int
    loading_at: datetime
    minutes_until: int
    label: str
    is_now: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _trip_minutes(direction: Direction) -> int:
    return max(30, int(direction.estimated_time_min or 60))


def _driver_rest_until(driver: DriverProfile, now: datetime) -> Optional[datetime]:
    until = getattr(driver, "rest_until", None)
    if not until:
        return None
    until = _aware(until)
    return until if until > now else None


def _order_for_driver_on_direction(driver_id: int, direction_id: int) -> Optional[Order]:
    return (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver_id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.direction_id == direction_id)
            & (
                Order.status.in_(
                    [OrderStatus.ASSIGNED.value, OrderStatus.IN_PROGRESS.value]
                )
            )
        )
        .order_by(Order.id.desc())
        .first()
    )


def _driver_frees_lane_at(
    driver: DriverProfile, direction: Direction, *, now: datetime
) -> datetime:
    """When this driver finishes loading + trip on the direction."""
    settings = get_settings()
    trip_min = _trip_minutes(direction)
    setup_min = settings.queue_loading_setup_min
    gap_min = settings.queue_loading_gap_min

    order = _order_for_driver_on_direction(driver.id, direction.id)
    if order and order.status == OrderStatus.IN_PROGRESS.value and order.started_at:
        started = _aware(order.started_at)
        trip_end = started + timedelta(minutes=trip_min)
        free_at = trip_end + timedelta(minutes=gap_min)
    elif order and order.status == OrderStatus.ASSIGNED.value:
        free_at = now + timedelta(minutes=setup_min + trip_min + gap_min)
    else:
        free_at = now

    rest = _driver_rest_until(driver, free_at)
    if rest and rest > free_at:
        free_at = rest
    return free_at


def _active_lane_blockers(direction_id: int, *, now: datetime) -> List[datetime]:
    """End times for drivers currently loading or in trip (not in queue table)."""
    direction = Direction.get_by_id(direction_id)
    ends: List[datetime] = []
    seen: set[int] = set()
    orders = Order.select().where(
        (Order.direction_id == direction_id)
        & (
            Order.status.in_(
                [OrderStatus.ASSIGNED.value, OrderStatus.IN_PROGRESS.value]
            )
        )
    )
    for order in orders:
        ass = (
            OrderDriverAssignment.select()
            .where(
                (OrderDriverAssignment.order_id == order.id)
                & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            )
            .first()
        )
        if not ass or ass.driver_id in seen:
            continue
        seen.add(ass.driver_id)
        driver = DriverProfile.get_by_id(ass.driver_id)
        ends.append(_driver_frees_lane_at(driver, direction, now=now))
    return sorted(ends)


def format_loading_label(loading_at: datetime, *, now: Optional[datetime] = None) -> str:
    now = now or _utcnow()
    loading_at = _aware(loading_at)
    delta_min = int((loading_at - now).total_seconds() // 60)
    if delta_min <= 5:
        return "сейчас"
    if delta_min < 90:
        return f"через {delta_min} мин"
    return f"через {minutes_to_hours_label(delta_min)}"


def compute_queue_schedule(direction_id: int) -> List[QueueEtaSlot]:
    direction = Direction.get_by_id(direction_id)
    now = _utcnow()
    trip_min = _trip_minutes(direction)
    gap_min = get_settings().queue_loading_gap_min

    slot_free_at = now
    for blocker_end in _active_lane_blockers(direction_id, now=now):
        if blocker_end > slot_free_at:
            slot_free_at = blocker_end

    rows = list(
        QueueEntry.select()
        .where(QueueEntry.direction_id == direction_id)
        .order_by(QueueEntry.position, QueueEntry.enqueued_at)
    )

    out: List[QueueEtaSlot] = []
    for row in rows:
        driver = DriverProfile.get_by_id(row.driver_id)
        rest = _driver_rest_until(driver, now)
        loading_at = slot_free_at
        if rest and rest > loading_at:
            loading_at = rest

        minutes_until = max(0, int((loading_at - now).total_seconds() // 60))
        label = format_loading_label(loading_at, now=now)
        is_now = minutes_until <= 5

        out.append(
            QueueEtaSlot(
                driver_id=driver.id,
                position=row.position,
                loading_at=loading_at,
                minutes_until=minutes_until,
                label=label,
                is_now=is_now,
            )
        )

        cycle_end = loading_at + timedelta(minutes=trip_min + gap_min)
        default_rest = get_settings().queue_default_rest_min
        if default_rest > 0:
            cycle_end = cycle_end + timedelta(minutes=default_rest)
        slot_free_at = cycle_end

    return out


def eta_for_driver(direction_id: int, driver_id: int) -> Optional[QueueEtaSlot]:
    for slot in compute_queue_schedule(direction_id):
        if slot.driver_id == driver_id:
            return slot
    return None

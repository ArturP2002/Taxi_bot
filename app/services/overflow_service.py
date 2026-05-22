"""Capacity / overflow detection for orders and directions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models import DriverProfile, Order, QueueEntry
from app.services import order_service
from app.services.loading_service import drivers_loading_on_direction, find_best_driver_for_order


@dataclass
class DirectionCapacityInfo:
    max_single_car_seats: int
    total_available_seats: int
    has_assignable_driver: bool


def direction_capacity_info(direction_id: int, *, excluded_driver_ids: Optional[set[int]] = None) -> DirectionCapacityInfo:
    excluded = excluded_driver_ids or set()
    max_single = 0
    total = 0
    has_driver = False

    for snap in drivers_loading_on_direction(direction_id):
        if snap.driver_id in excluded:
            continue
        max_single = max(max_single, snap.free_seats)
        total += snap.free_seats
        if snap.free_seats > 0:
            has_driver = True

    for row in (
        QueueEntry.select(QueueEntry, DriverProfile)
        .join(DriverProfile, on=(QueueEntry.driver_id == DriverProfile.id))
        .where(
            (QueueEntry.direction_id == direction_id)
            & (DriverProfile.online == True)  # noqa: E712
            & (DriverProfile.status == "active")
        )
        .order_by(QueueEntry.position, QueueEntry.enqueued_at)
    ):
        drv = row.driver
        if drv.id in excluded:
            continue
        free = order_service.platform_capacity_remaining(drv)
        max_single = max(max_single, free)
        total += free
        if free > 0:
            has_driver = True

    return DirectionCapacityInfo(
        max_single_car_seats=max_single,
        total_available_seats=total,
        has_assignable_driver=has_driver,
    )


def order_fits_any_driver(order: Order, *, excluded: Optional[set[int]] = None) -> bool:
    return find_best_driver_for_order(order, excluded=excluded or set()) is not None


def order_has_overflow(order: Order, *, excluded: Optional[set[int]] = None) -> bool:
    """
    True when the order needs more seats than any single car can offer (перебор).
    Does not depend on auto-assign: 5 мест при max 5 в машине — не SOS.
    """
    info = direction_capacity_info(order.direction_id, excluded_driver_ids=excluded)
    return order.seats > info.max_single_car_seats


def mark_order_overflow_review(order: Order) -> None:
    from datetime import datetime, timezone

    from app.models import Order as OrderModel, OrderStatus

    now = datetime.now(timezone.utc)
    OrderModel.update(status=OrderStatus.ADMIN_REVIEW.value, updated_at=now).where(
        OrderModel.id == order.id
    ).execute()

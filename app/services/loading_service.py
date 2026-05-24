"""Passenger loading per driver and direction (набор / перебор / очередь)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.models import (
    AssignmentStatus,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    QueueEntry,
)
from app.services import order_service


WAITING_ORDER_STATUSES = (
    OrderStatus.NEW.value,
    OrderStatus.ADMIN_REVIEW.value,
    OrderStatus.AWAITING_PAYMENT.value,
)


@dataclass
class PassengerSlot:
    order_id: int
    seats: int
    from_location: str
    to_location: str
    status: str
    pickup_location: Optional[str] = None
    pickup_time_text: Optional[str] = None


@dataclass
class DriverLoadingSnapshot:
    driver_id: int
    full_name: Optional[str]
    car_info: Optional[str]
    max_seats: int
    own_seats: int
    occupied_seats: int
    free_seats: int
    in_trip: bool
    loading: bool
    online: bool
    status_label: str
    passengers: List[PassengerSlot]


def _passenger_slots(driver_id: int) -> List[PassengerSlot]:
    rows = (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver_id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status.in_(list(order_service.ACTIVE_ORDER_STATUSES)))
        )
        .order_by(Order.id)
    )
    return [
        PassengerSlot(
            order_id=o.id,
            seats=o.seats,
            from_location=o.from_location,
            to_location=o.to_location,
            status=o.status,
            pickup_location=o.pickup_location,
            pickup_time_text=o.pickup_time_text,
        )
        for o in rows
    ]


def driver_loading_snapshot(driver: DriverProfile, *, in_trip: bool = False) -> DriverLoadingSnapshot:
    own = int(getattr(driver, "own_seats_reserved", 0) or 0)
    occupied = order_service.occupied_seats_for_driver(driver)
    free = order_service.platform_capacity_remaining(driver)
    passengers = _passenger_slots(driver.id)

    if in_trip:
        label = f"В пути · {occupied} мест"
    elif free <= 0:
        label = f"Машина полная · {occupied}/{driver.max_seats}"
    elif occupied > 0:
        label = f"Набор · занято {occupied}, свободно {free}"
    else:
        label = f"Свободно {free} мест"

    return DriverLoadingSnapshot(
        driver_id=driver.id,
        full_name=driver.full_name,
        car_info=driver.car_info,
        max_seats=driver.max_seats,
        own_seats=own,
        occupied_seats=occupied,
        free_seats=free,
        in_trip=in_trip,
        loading=bool(getattr(driver, "loading", False)),
        online=bool(driver.online),
        status_label=label,
        passengers=passengers,
    )


def direction_waiting_pool(direction_id: int) -> Dict[str, Any]:
    """Passengers without a driver yet (new / admin review / awaiting pay)."""
    orders = list(
        Order.select()
        .where(
            (Order.direction_id == direction_id)
            & (Order.status.in_(list(WAITING_ORDER_STATUSES)))
        )
        .order_by(Order.id)
    )
    from app.services import scheduled_trip_service

    ready = [
        o
        for o in orders
        if scheduled_trip_service.is_order_in_live_queue(o)
        and (order_service.order_ready_for_dispatch(o) or o.status == OrderStatus.ADMIN_REVIEW.value)
    ]
    total_seats = sum(o.seats for o in ready)
    return {
        "order_count": len(ready),
        "total_seats": total_seats,
        "orders": [
            {
                "id": o.id,
                "seats": o.seats,
                "from_location": o.from_location,
                "to_location": o.to_location,
                "status": o.status,
                "phone": o.phone,
            }
            for o in ready
        ],
    }


def drivers_loading_on_direction(direction_id: int) -> List[DriverLoadingSnapshot]:
    """Drivers already taking passengers on this route (may still have free seats)."""
    assignments = (
        OrderDriverAssignment.select(OrderDriverAssignment, Order)
        .join(Order)
        .where(
            (Order.direction_id == direction_id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.ASSIGNED.value)
        )
    )
    seen: set[int] = set()
    out: List[DriverLoadingSnapshot] = []
    for ass in assignments:
        if ass.driver_id in seen:
            continue
        seen.add(ass.driver_id)
        drv = DriverProfile.get_by_id(ass.driver_id)
        out.append(driver_loading_snapshot(drv, in_trip=False))
    out.sort(key=lambda s: (-s.occupied_seats, s.driver_id))
    return out


def find_best_driver_for_order(
    order: Order, *, excluded: Optional[set[int]] = None
) -> Optional[DriverProfile]:
    """Fill loading cars first, then FIFO queue; skip if no capacity (перебор)."""
    excluded = excluded or set()
    direction_id = order.direction_id

    loading = drivers_loading_on_direction(direction_id)
    for snap in loading:
        if snap.driver_id in excluded or snap.free_seats < order.seats:
            continue
        drv = DriverProfile.get_by_id(snap.driver_id)
        if drv.direction_id != order.direction_id:
            continue
        if order_service.can_assign_order(drv, order):
            return drv

    from app.models import QueueEntry

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
        if drv.direction_id != order.direction_id:
            continue
        if order_service.can_assign_order(drv, order):
            return drv
    return None


def snapshot_to_dict(s: DriverLoadingSnapshot) -> Dict[str, Any]:
    return {
        "driver_id": s.driver_id,
        "full_name": s.full_name,
        "car_info": s.car_info,
        "max_seats": s.max_seats,
        "own_seats": s.own_seats,
        "occupied_seats": s.occupied_seats,
        "free_seats": s.free_seats,
        "in_trip": s.in_trip,
        "loading": s.loading,
        "online": s.online,
        "status_label": s.status_label,
        "passengers": [
            {
                "order_id": p.order_id,
                "seats": p.seats,
                "from_location": p.from_location,
                "to_location": p.to_location,
                "status": p.status,
                "pickup_location": p.pickup_location,
                "pickup_time_text": p.pickup_time_text,
            }
            for p in s.passengers
        ],
    }

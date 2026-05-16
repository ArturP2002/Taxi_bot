from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

from peewee import fn

from app.config import get_settings
from app.models import (
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    AssignmentStatus,
)
from app.services import code_service, commission_service, queue_service, audit_service


ACTIVE_ORDER_STATUSES = (
    OrderStatus.ASSIGNED.value,
    OrderStatus.IN_PROGRESS.value,
)


def _assignment_driver_pk(ass: OrderDriverAssignment) -> int:
    d = ass.driver
    return int(d) if isinstance(d, int) else d.id


def occupied_seats_for_driver(driver: DriverProfile) -> int:
    q = (
        Order.select(fn.SUM(Order.seats))
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status.in_(list(ACTIVE_ORDER_STATUSES)))
        )
        .scalar()
    )
    return int(q or 0)


def can_assign_order(driver: DriverProfile, order: Order) -> bool:
    occ = occupied_seats_for_driver(driver)
    return occ + order.seats <= driver.max_seats


def assign_order_to_driver(
    order: Order,
    driver: DriverProfile,
    *,
    pickup_location: Optional[str] = None,
    pickup_time_text: Optional[str] = None,
    actor_telegram_id: Optional[int] = None,
) -> OrderDriverAssignment:
    if not can_assign_order(driver, order):
        raise ValueError("capacity_exceeded")
    if order.direction_id != driver.direction_id:
        raise ValueError("direction_mismatch")
    d = Direction.get_by_id(order.direction_id)
    queue_service.remove_from_queue(d, driver)
    now = datetime.now(timezone.utc)
    OrderDriverAssignment.update(status=AssignmentStatus.DECLINED.value).where(
        (OrderDriverAssignment.order_id == order.id) & (OrderDriverAssignment.status == AssignmentStatus.PENDING.value)
    ).execute()
    ass = OrderDriverAssignment.create(
        order=order,
        driver=driver,
        status=AssignmentStatus.PENDING.value,
        assigned_at=now,
    )
    Order.update(
        status=OrderStatus.ASSIGNED.value,
        pickup_location=pickup_location,
        pickup_time_text=pickup_time_text,
        updated_at=now,
    ).where(Order.id == order.id).execute()
    audit_service.log_action(
        "order_assigned",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
        payload={"driver_id": driver.id},
    )
    return ass


def driver_respond(assignment: OrderDriverAssignment, accept: bool) -> Order:
    now = datetime.now(timezone.utc)
    order = assignment.order
    driver = assignment.driver
    if accept:
        OrderDriverAssignment.update(
            status=AssignmentStatus.ACCEPTED.value, responded_at=now
        ).where(OrderDriverAssignment.id == assignment.id).execute()
        audit_service.log_action(
            "order_accepted",
            actor_telegram_id=driver.user.telegram_id,
            entity_type="order",
            entity_id=str(order.id),
        )
    else:
        OrderDriverAssignment.update(
            status=AssignmentStatus.DECLINED.value, responded_at=now
        ).where(OrderDriverAssignment.id == assignment.id).execute()
        Order.update(status=OrderStatus.ADMIN_REVIEW.value, updated_at=now).where(Order.id == order.id).execute()
        audit_service.log_action(
            "order_declined",
            actor_telegram_id=driver.user.telegram_id,
            entity_type="order",
            entity_id=str(order.id),
        )
    return Order.get_by_id(order.id)


def get_active_assignment(order: Order) -> Optional[OrderDriverAssignment]:
    return (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == order.id)
            & (OrderDriverAssignment.status == AssignmentStatus.PENDING.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )


def get_accepted_assignment(order: Order) -> Optional[OrderDriverAssignment]:
    return (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == order.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )


def verify_order_code(order: Order, code_or_token: str) -> Tuple[bool, str]:
    """Returns (ok, message_key)."""
    if order.code_consumed_at:
        return False, "already_used"
    ass = get_accepted_assignment(order)
    if not ass:
        return False, "no_active_assignment"
    if order.status != OrderStatus.ASSIGNED.value:
        return False, "bad_status"

    raw = code_or_token.strip()
    order_id = order.id
    if len(raw) > 6:
        oid = code_service.verify_qr_token(raw)
        if oid != order_id:
            return False, "invalid_token"
    else:
        if not code_service.verify_code(order_id, raw, order.confirmation_code_hash):
            return False, "invalid_code"

    now = datetime.now(timezone.utc)
    Order.update(
        status=OrderStatus.IN_PROGRESS.value,
        code_consumed_at=now,
        started_at=now,
        updated_at=now,
    ).where(Order.id == order.id).execute()
    audit_service.log_action(
        "code_verified",
        entity_type="order",
        entity_id=str(order.id),
        payload={"driver_id": _assignment_driver_pk(ass)},
    )
    return True, "ok"


def min_trip_seconds(direction: Direction) -> int:
    base_min = direction.estimated_time_min
    pct = direction.min_time_percent / 100.0
    return int(base_min * 60 * pct)


def complete_order(order: Order, driver: DriverProfile) -> Tuple[bool, str]:
    if order.status != OrderStatus.IN_PROGRESS.value:
        return False, "not_in_progress"
    ass = get_accepted_assignment(order)
    if not ass or _assignment_driver_pk(ass) != driver.id:
        return False, "not_your_order"
    if not order.started_at:
        return False, "no_start_time"
    direction = order.direction
    need = min_trip_seconds(direction)
    now = datetime.now(timezone.utc)
    started = order.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (now - started).total_seconds() < need:
        return False, "too_early"

    Order.update(status=OrderStatus.COMPLETED.value, ended_at=now, updated_at=now).where(Order.id == order.id).execute()
    commission_service.record_commission(order, driver)

    # Return queue: if driver requested reverse direction during trip
    rev = driver.pending_return_direction_id
    DriverProfile.update(pending_return_direction=None).where(DriverProfile.id == driver.id).execute()
    if rev:
        d = Direction.get_by_id(rev)
        queue_service.enqueue_driver_end(d, driver)
    elif driver.direction_id:
        d = Direction.get_by_id(driver.direction_id)
        queue_service.enqueue_driver_end(d, driver)

    audit_service.log_action(
        "order_completed",
        actor_telegram_id=driver.user.telegram_id,
        entity_type="order",
        entity_id=str(order.id),
    )
    return True, "ok"


def debt_level(balance: Decimal) -> str:
    s = get_settings()
    b = balance
    if b >= Decimal(s.debt_block):
        return "block"
    if b >= Decimal(s.debt_restrict):
        return "restrict"
    if b >= Decimal(s.debt_warn):
        return "warn"
    return "ok"


def _declined_driver_ids(order: Order) -> List[int]:
    rows = OrderDriverAssignment.select(OrderDriverAssignment.driver).where(
        (OrderDriverAssignment.order_id == order.id)
        & (OrderDriverAssignment.status.in_([
            AssignmentStatus.DECLINED.value,
        ]))
    )
    return [_assignment_driver_pk(r) for r in rows]


def suggest_driver_for_order(order: Order) -> Optional[OrderDriverAssignment]:
    """Pick the best eligible driver from FIFO queue and create a SUGGESTED assignment."""
    direction = order.direction
    excluded = set(_declined_driver_ids(order))

    from app.models import QueueEntry
    candidates = (
        QueueEntry.select(QueueEntry, DriverProfile)
        .join(DriverProfile, on=(QueueEntry.driver_id == DriverProfile.id))
        .where(
            (QueueEntry.direction_id == direction.id)
            & (DriverProfile.online == True)  # noqa: E712
            & (DriverProfile.status == "active")
        )
        .order_by(QueueEntry.position, QueueEntry.enqueued_at)
    )
    for row in candidates:
        drv = row.driver
        if drv.id in excluded:
            continue
        if not can_assign_order(drv, order):
            continue
        if order.direction_id != drv.direction_id:
            continue

        now = datetime.now(timezone.utc)
        OrderDriverAssignment.update(
            status=AssignmentStatus.DECLINED.value,
        ).where(
            (OrderDriverAssignment.order_id == order.id)
            & (OrderDriverAssignment.status == AssignmentStatus.SUGGESTED.value)
        ).execute()

        ass = OrderDriverAssignment.create(
            order=order,
            driver=drv,
            status=AssignmentStatus.SUGGESTED.value,
            assigned_at=now,
        )
        audit_service.log_action(
            "driver_suggested",
            entity_type="order",
            entity_id=str(order.id),
            payload={"driver_id": drv.id},
        )
        return ass
    return None


def get_suggestion(order: Order) -> Optional[OrderDriverAssignment]:
    return (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == order.id)
            & (OrderDriverAssignment.status == AssignmentStatus.SUGGESTED.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )


def confirm_suggestion(
    assignment: OrderDriverAssignment,
    *,
    pickup_location: Optional[str] = None,
    pickup_time_text: Optional[str] = None,
    actor_telegram_id: Optional[int] = None,
) -> OrderDriverAssignment:
    """Admin confirms the system-suggested driver — performs the real assignment."""
    order = Order.get_by_id(assignment.order_id)
    driver = DriverProfile.get_by_id(assignment.driver_id)

    if not can_assign_order(driver, order):
        raise ValueError("capacity_exceeded")
    if order.direction_id != driver.direction_id:
        raise ValueError("direction_mismatch")
    if not driver.online:
        raise ValueError("driver_offline")

    d = Direction.get_by_id(order.direction_id)
    queue_service.remove_from_queue(d, driver)

    now = datetime.now(timezone.utc)
    OrderDriverAssignment.update(
        status=AssignmentStatus.PENDING.value,
        assigned_at=now,
    ).where(OrderDriverAssignment.id == assignment.id).execute()

    Order.update(
        status=OrderStatus.ASSIGNED.value,
        pickup_location=pickup_location,
        pickup_time_text=pickup_time_text,
        updated_at=now,
    ).where(Order.id == order.id).execute()

    audit_service.log_action(
        "suggestion_confirmed",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
        payload={"driver_id": driver.id},
    )
    return OrderDriverAssignment.get_by_id(assignment.id)


def reject_suggestion(
    assignment: OrderDriverAssignment,
    *,
    actor_telegram_id: Optional[int] = None,
) -> Optional[OrderDriverAssignment]:
    """Admin rejects suggested driver. Returns next suggestion if available."""
    now = datetime.now(timezone.utc)
    OrderDriverAssignment.update(
        status=AssignmentStatus.DECLINED.value,
        responded_at=now,
    ).where(OrderDriverAssignment.id == assignment.id).execute()

    order = Order.get_by_id(assignment.order_id)
    audit_service.log_action(
        "suggestion_rejected",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
        payload={"driver_id": assignment.driver_id},
    )
    return suggest_driver_for_order(order)

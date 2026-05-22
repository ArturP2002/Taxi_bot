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
    PassengerPaymentStatus,
)
from app.services import code_service, commission_service, queue_service, audit_service


ACTIVE_ORDER_STATUSES = (
    OrderStatus.ASSIGNED.value,
    OrderStatus.IN_PROGRESS.value,
)


def _assignment_driver_pk(ass: OrderDriverAssignment) -> int:
    d = ass.driver
    return int(d) if isinstance(d, int) else d.id


def platform_capacity_remaining(driver: DriverProfile) -> int:
    own = int(getattr(driver, "own_seats_reserved", 0) or 0)
    occ = occupied_seats_for_driver(driver)
    return max(0, driver.max_seats - own - occ)


def compute_platform_seats(order: Order, driver: DriverProfile) -> int:
    remaining = platform_capacity_remaining(driver)
    return min(order.seats, remaining) if remaining > 0 else 0


def occupied_seats_for_driver(driver: DriverProfile) -> int:
    q = (
        Order.select(fn.SUM(Order.seats))
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver.id)
            & (
                OrderDriverAssignment.status.in_(
                    [
                        AssignmentStatus.ACCEPTED.value,
                        AssignmentStatus.PENDING.value,
                    ]
                )
            )
            & (Order.status.in_(list(ACTIVE_ORDER_STATUSES)))
        )
        .scalar()
    )
    return int(q or 0)


def can_assign_order(driver: DriverProfile, order: Order) -> bool:
    return compute_platform_seats(order, driver) >= order.seats


def _set_platform_seats(order: Order, driver: DriverProfile) -> None:
    ps = compute_platform_seats(order, driver)
    Order.update(platform_seats=ps).where(Order.id == order.id).execute()


def update_driver_loading(driver: DriverProfile) -> None:
    has_accepted = (
        OrderDriverAssignment.select()
        .join(Order)
        .where(
            (OrderDriverAssignment.driver_id == driver.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.ASSIGNED.value)
        )
        .exists()
    )
    DriverProfile.update(loading=has_accepted).where(DriverProfile.id == driver.id).execute()


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
    _set_platform_seats(order, driver)
    Order.update(
        status=OrderStatus.ASSIGNED.value,
        pickup_location=pickup_location,
        pickup_time_text=pickup_time_text,
        updated_at=now,
    ).where(Order.id == order.id).execute()
    update_driver_loading(driver)
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
        if not can_assign_order(driver, order):
            OrderDriverAssignment.update(
                status=AssignmentStatus.DECLINED.value, responded_at=now
            ).where(OrderDriverAssignment.id == assignment.id).execute()
            Order.update(status=OrderStatus.ADMIN_REVIEW.value, updated_at=now).where(
                Order.id == order.id
            ).execute()
            raise ValueError("capacity_exceeded")
        OrderDriverAssignment.update(
            status=AssignmentStatus.ACCEPTED.value, responded_at=now
        ).where(OrderDriverAssignment.id == assignment.id).execute()
        update_driver_loading(driver)
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
        update_driver_loading(driver)
        audit_service.log_action(
            "order_declined",
            actor_telegram_id=driver.user.telegram_id,
            entity_type="order",
            entity_id=str(order.id),
        )
        from app.services import driver_risk_service

        driver_risk_service.record_decline(driver.id, order.id)
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


def _validate_boarding_code(
    order: Order,
    code_or_token: str,
    *,
    expected_order_id: Optional[int] = None,
) -> Tuple[bool, str]:
    parsed = code_service.parse_verification_raw(
        code_or_token,
        default_order_id=expected_order_id or order.id,
    )
    if not parsed:
        return False, "invalid_format"
    if parsed.order_id != order.id:
        return False, "wrong_order"

    if parsed.source == "jwt":
        oid = code_service.verify_qr_token(code_or_token.strip())
        if oid != order.id:
            return False, "invalid_token"
    elif parsed.code:
        if not code_service.verify_code(
            order.id, parsed.code, order.confirmation_code_hash
        ):
            return False, "invalid_code"
    else:
        return False, "invalid_format"
    return True, "ok"


def driver_accepted_orders(driver: DriverProfile, *, assigned_only: bool = False) -> List[Order]:
    statuses = [OrderStatus.ASSIGNED.value]
    if not assigned_only:
        statuses.append(OrderStatus.IN_PROGRESS.value)
    return list(
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status.in_(statuses))
        )
        .order_by(Order.id)
    )


def driver_boarding_summary(driver: DriverProfile) -> dict:
    """Loading state: boarded vs waiting, free seats."""
    orders = driver_accepted_orders(driver, assigned_only=True)
    boarded = [o for o in orders if o.code_consumed_at]
    waiting = [o for o in orders if not o.code_consumed_at]
    occupied = sum(o.seats for o in boarded)
    free = platform_capacity_remaining(driver)
    in_trip = (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.IN_PROGRESS.value)
        )
        .exists()
    )
    return {
        "orders": orders,
        "boarded": boarded,
        "waiting_boarding": waiting,
        "boarded_seats": occupied,
        "free_seats": free,
        "in_trip": in_trip,
    }


def verify_passenger_boarding(
    order: Order,
    code_or_token: str,
    *,
    driver_id: int,
    expected_order_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Mark passenger as boarded (code/QR ok). Order stays ASSIGNED until departure.
    """
    if order.code_consumed_at:
        if order.status == OrderStatus.ASSIGNED.value:
            return False, "already_boarded"
        return False, "already_used"
    ass = get_accepted_assignment(order)
    if not ass or _assignment_driver_pk(ass) != driver_id:
        return False, "not_your_order"
    if order.status == OrderStatus.IN_PROGRESS.value:
        return False, "already_departed"
    if order.status != OrderStatus.ASSIGNED.value:
        return False, "bad_status"

    ok, key = _validate_boarding_code(
        order, code_or_token, expected_order_id=expected_order_id
    )
    if not ok:
        return False, key

    driver = DriverProfile.get_by_id(driver_id)
    now = datetime.now(timezone.utc)
    Order.update(
        code_consumed_at=now,
        boarding_code=None,
        updated_at=now,
    ).where(Order.id == order.id).execute()
    commission_service.record_commission(order, driver, on_start=True)
    update_driver_loading(driver)
    audit_service.log_action(
        "passenger_boarded",
        entity_type="order",
        entity_id=str(order.id),
        payload={"driver_id": driver_id},
    )
    return True, "boarded"


def depart_driver_trip(driver: DriverProfile) -> Tuple[bool, str, dict]:
    """
    Start trip: all boarded (code confirmed) orders → IN_PROGRESS.
    """
    summary = driver_boarding_summary(driver)
    if summary["in_trip"]:
        return False, "trip_already_started", summary

    boarded: List[Order] = summary["boarded"]
    if not boarded:
        return False, "no_boarded_passengers", summary

    now = datetime.now(timezone.utc)
    for o in boarded:
        Order.update(
            status=OrderStatus.IN_PROGRESS.value,
            started_at=now,
            updated_at=now,
        ).where(Order.id == o.id).execute()
        audit_service.log_action(
            "trip_departed",
            actor_telegram_id=driver.user.telegram_id,
            entity_type="order",
            entity_id=str(o.id),
            payload={"driver_id": driver.id},
        )

    update_driver_loading(driver)
    return True, "ok", {
        "departed_orders": boarded,
        "waiting_boarding": summary["waiting_boarding"],
        "free_seats_at_depart": summary["free_seats"],
        "boarded_seats": summary["boarded_seats"],
    }


def verify_order_code(
    order: Order,
    code_or_token: str,
    *,
    expected_order_id: Optional[int] = None,
    driver_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """Backward-compatible alias: boarding only, not departure."""
    if driver_id is None:
        ass = get_accepted_assignment(order)
        if not ass:
            return False, "no_active_assignment"
        driver_id = _assignment_driver_pk(ass)
    return verify_passenger_boarding(
        order,
        code_or_token,
        driver_id=driver_id,
        expected_order_id=expected_order_id,
    )


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
    update_driver_loading(driver)

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
    from app.services import driver_risk_service

    driver_risk_service.record_trip_completed(driver.id, order.id)
    return True, "ok"


def cancel_order(order: Order, *, actor_telegram_id: Optional[int] = None) -> Optional[int]:
    """Cancel order. Returns driver_id if a linked driver should be flagged for risk."""
    now = datetime.now(timezone.utc)
    from app.services import driver_risk_service

    linked_driver_id = driver_risk_service.driver_linked_to_cancelled_order(order)
    Order.update(status=OrderStatus.CANCELLED.value, updated_at=now).where(Order.id == order.id).execute()
    audit_service.log_action(
        "order_cancelled",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
        payload={"linked_driver_id": linked_driver_id} if linked_driver_id else None,
    )
    if linked_driver_id:
        driver_risk_service.record_order_cancelled_for_driver(linked_driver_id, order.id)
    return linked_driver_id


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
        & (OrderDriverAssignment.status.in_([AssignmentStatus.DECLINED.value]))
    )
    return [_assignment_driver_pk(r) for r in rows]


def _busy_suggested_driver_ids() -> set[int]:
    rows = OrderDriverAssignment.select(OrderDriverAssignment.driver_id).where(
        OrderDriverAssignment.status == AssignmentStatus.SUGGESTED.value
    )
    return {r.driver_id for r in rows}


def order_ready_for_dispatch(order: Order) -> bool:
    if order.status != OrderStatus.NEW.value:
        if order.status == OrderStatus.AWAITING_PAYMENT.value:
            return order.passenger_payment_status == PassengerPaymentStatus.PAID.value
        return False
    if order.passenger_payment_status == PassengerPaymentStatus.AWAITING.value:
        return False
    return True


def decline_suggested_assignments(
    *,
    order_id: Optional[int] = None,
    driver_id: Optional[int] = None,
) -> int:
    """Cancel stale SUGGESTED rows (e.g. after admin changed driver direction)."""
    now = datetime.now(timezone.utc)
    q = OrderDriverAssignment.update(
        status=AssignmentStatus.DECLINED.value,
        responded_at=now,
    ).where(OrderDriverAssignment.status == AssignmentStatus.SUGGESTED.value)
    if order_id is not None:
        q = q.where(OrderDriverAssignment.order_id == order_id)
    if driver_id is not None:
        q = q.where(OrderDriverAssignment.driver_id == driver_id)
    return q.execute()


def suggest_driver_for_order(order: Order) -> Optional[OrderDriverAssignment]:
    if not order_ready_for_dispatch(order):
        return None
    excluded = set(_declined_driver_ids(order)) | _busy_suggested_driver_ids()

    from app.services.loading_service import find_best_driver_for_order

    drv = find_best_driver_for_order(order, excluded=excluded)
    if drv and drv.direction_id != order.direction_id:
        excluded.add(drv.id)
        drv = find_best_driver_for_order(order, excluded=excluded)
    if drv:
        now = datetime.now(timezone.utc)
        OrderDriverAssignment.update(status=AssignmentStatus.DECLINED.value).where(
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


def auto_assign_pending_orders(driver: DriverProfile) -> List[Order]:
    """Assign more new orders to driver if auto_assign enabled and capacity allows."""
    if not get_settings().auto_assign_enabled:
        return []
    if not driver.direction_id or driver.status != "active":
        return []
    assigned: List[Order] = []
    while True:
        occ = occupied_seats_for_driver(driver)
        pending_count = (
            OrderDriverAssignment.select()
            .where(
                (OrderDriverAssignment.driver_id == driver.id)
                & (OrderDriverAssignment.status == AssignmentStatus.PENDING.value)
            )
            .count()
        )
        own = int(getattr(driver, "own_seats_reserved", 0) or 0)
        free = driver.max_seats - own - occ - pending_count
        if free <= 0:
            break
        order = (
            Order.select()
            .where(
                (Order.direction_id == driver.direction_id)
                & (Order.status == OrderStatus.NEW.value)
            )
            .order_by(Order.id)
            .first()
        )
        if not order or not order_ready_for_dispatch(order):
            break
        if order.seats > free:
            break
        try:
            assign_order_to_driver(order, driver)
            assigned.append(order)
        except ValueError:
            break
    return assigned


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
    order = Order.get_by_id(assignment.order_id)
    driver = DriverProfile.get_by_id(assignment.driver_id)

    if not can_assign_order(driver, order):
        raise ValueError("capacity_exceeded")
    if order.direction_id != driver.direction_id:
        decline_suggested_assignments(order_id=order.id)
        suggest_driver_for_order(order)
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

    _set_platform_seats(order, driver)
    Order.update(
        status=OrderStatus.ASSIGNED.value,
        pickup_location=pickup_location,
        pickup_time_text=pickup_time_text,
        updated_at=now,
    ).where(Order.id == order.id).execute()
    update_driver_loading(driver)

    audit_service.log_action(
        "suggestion_confirmed",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
        payload={"driver_id": driver.id},
    )
    auto_assign_pending_orders(driver)
    return OrderDriverAssignment.get_by_id(assignment.id)


def reject_suggestion(
    assignment: OrderDriverAssignment,
    *,
    actor_telegram_id: Optional[int] = None,
) -> Optional[OrderDriverAssignment]:
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


def unassign_order_from_driver(
    order: Order,
    *,
    actor_telegram_id: Optional[int] = None,
) -> Optional[int]:
    """Decline active pending/accepted assignments. Returns previous driver_id."""
    now = datetime.now(timezone.utc)
    prev = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == order.id)
            & (
                OrderDriverAssignment.status.in_(
                    [
                        AssignmentStatus.PENDING.value,
                        AssignmentStatus.ACCEPTED.value,
                    ]
                )
            )
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )
    if not prev:
        return None
    driver_id = _assignment_driver_pk(prev)
    OrderDriverAssignment.update(
        status=AssignmentStatus.DECLINED.value,
        responded_at=now,
    ).where(
        (OrderDriverAssignment.order_id == order.id)
        & (
            OrderDriverAssignment.status.in_(
                [
                    AssignmentStatus.PENDING.value,
                    AssignmentStatus.ACCEPTED.value,
                    AssignmentStatus.SUGGESTED.value,
                ]
            )
        )
    ).execute()
    driver = DriverProfile.get_by_id(driver_id)
    update_driver_loading(driver)
    if order.status in (OrderStatus.ASSIGNED.value, OrderStatus.IN_PROGRESS.value):
        Order.update(status=OrderStatus.ADMIN_REVIEW.value, updated_at=now).where(
            Order.id == order.id
        ).execute()
    audit_service.log_action(
        "order_unassigned",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
        payload={"driver_id": driver_id},
    )
    return driver_id


def reassign_order(
    order: Order,
    new_driver: DriverProfile,
    *,
    pickup_location: Optional[str] = None,
    pickup_time_text: Optional[str] = None,
    actor_telegram_id: Optional[int] = None,
) -> OrderDriverAssignment:
    old_driver_id = unassign_order_from_driver(order, actor_telegram_id=actor_telegram_id)
    order = Order.get_by_id(order.id)
    ass = assign_order_to_driver(
        order,
        new_driver,
        pickup_location=pickup_location,
        pickup_time_text=pickup_time_text,
        actor_telegram_id=actor_telegram_id,
    )
    audit_service.log_action(
        "order_reassigned",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
        payload={"from_driver_id": old_driver_id, "to_driver_id": new_driver.id},
    )
    return ass


def list_order_assignments(order_id: int) -> List[OrderDriverAssignment]:
    return list(
        OrderDriverAssignment.select()
        .where(OrderDriverAssignment.order_id == order_id)
        .order_by(OrderDriverAssignment.assigned_at)
    )

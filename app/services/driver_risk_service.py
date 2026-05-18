"""Driver risk: declines, linked cancellations, suspicious status."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from app.config import get_settings
from app.models import (
    AssignmentStatus,
    DriverEvent,
    DriverEventType,
    DriverProfile,
    DriverStatus,
    Order,
    OrderDriverAssignment,
)
from app.util.datetimeutil import utcnow

logger = logging.getLogger("taxi_bot.driver_risk")


def _since_days(days: int):
    return utcnow() - timedelta(days=days)


def record_event(
    driver_id: int,
    event_type: str,
    *,
    order_id: Optional[int] = None,
) -> None:
    DriverEvent.create(driver_id=driver_id, order_id=order_id, event_type=event_type)


def _count_events(driver_id: int, event_type: str, *, days: int = 30) -> int:
    since = _since_days(days)
    return (
        DriverEvent.select()
        .where(
            (DriverEvent.driver_id == driver_id)
            & (DriverEvent.event_type == event_type)
            & (DriverEvent.created_at >= since)
        )
        .count()
    )


def driver_risk_stats(driver_id: int, *, days: int = 30) -> dict[str, Any]:
    declines = _count_events(driver_id, DriverEventType.DECLINE.value, days=days)
    cancels = _count_events(driver_id, DriverEventType.ORDER_CANCELLED.value, days=days)
    completed = _count_events(driver_id, DriverEventType.TRIP_COMPLETED.value, days=days)
    total_actions = declines + cancels + completed
    decline_rate = round(declines / total_actions, 2) if total_actions else 0.0
    return {
        "days": days,
        "declines": declines,
        "order_cancellations": cancels,
        "trips_completed": completed,
        "decline_rate": decline_rate,
        "risk_label": _risk_label(declines, cancels, completed, decline_rate),
    }


def _risk_label(declines: int, cancels: int, completed: int, decline_rate: float) -> str:
    s = get_settings()
    if declines >= s.driver_declines_suspicious_30d:
        return "high_declines"
    if cancels >= s.driver_cancels_suspicious_30d:
        return "high_cancellations"
    if declines >= 3 and decline_rate >= s.driver_decline_rate_suspicious:
        return "high_decline_rate"
    if completed == 0 and (declines + cancels) >= 2:
        return "no_completions"
    return "ok"


def should_be_suspicious(stats: dict[str, Any]) -> bool:
    return stats.get("risk_label") != "ok"


def evaluate_driver(driver: DriverProfile) -> bool:
    """Mark driver suspicious if thresholds exceeded. Returns True if newly marked."""
    if driver.status in (DriverStatus.BLOCKED.value, DriverStatus.PENDING.value):
        return False
    stats = driver_risk_stats(driver.id)
    if not should_be_suspicious(stats):
        return False
    if driver.status == DriverStatus.SUSPICIOUS.value:
        return False
    DriverProfile.update(
        status=DriverStatus.SUSPICIOUS.value,
        online=False,
    ).where(DriverProfile.id == driver.id).execute()
    driver = DriverProfile.get_by_id(driver.id)
    if driver.direction_id:
        from app.models import Direction
        from app.services import queue_service

        queue_service.remove_from_queue(Direction.get_by_id(driver.direction_id), driver)
    logger.warning(
        "Driver %s marked suspicious: declines=%s cancels=%s completed=%s",
        driver.id,
        stats["declines"],
        stats["order_cancellations"],
        stats["trips_completed"],
    )
    return True


def record_decline(driver_id: int, order_id: int) -> bool:
    record_event(driver_id, DriverEventType.DECLINE.value, order_id=order_id)
    return evaluate_driver(DriverProfile.get_by_id(driver_id))


def record_order_cancelled_for_driver(driver_id: int, order_id: int) -> bool:
    record_event(driver_id, DriverEventType.ORDER_CANCELLED.value, order_id=order_id)
    return evaluate_driver(DriverProfile.get_by_id(driver_id))


def record_trip_completed(driver_id: int, order_id: int) -> None:
    record_event(driver_id, DriverEventType.TRIP_COMPLETED.value, order_id=order_id)


def driver_linked_to_cancelled_order(order: Order) -> Optional[int]:
    """Driver who accepted or was assigned when order is cancelled."""
    ass = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == order.id)
            & (
                OrderDriverAssignment.status.in_(
                    [
                        AssignmentStatus.ACCEPTED.value,
                        AssignmentStatus.PENDING.value,
                    ]
                )
            )
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )
    if ass:
        return ass.driver_id
    return None


def is_operational(driver: DriverProfile) -> bool:
    return driver.status == DriverStatus.ACTIVE.value


def clear_suspicious(driver_id: int) -> None:
    DriverProfile.update(status=DriverStatus.ACTIVE.value).where(
        (DriverProfile.id == driver_id)
        & (DriverProfile.status == DriverStatus.SUSPICIOUS.value)
    ).execute()

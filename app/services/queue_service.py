from datetime import datetime, timezone
from typing import List, Optional

from peewee import fn

from app.models import Direction, DriverProfile, QueueEntry
from app.services import audit_service


def _next_position(direction_id: int) -> int:
    q = QueueEntry.select(fn.MAX(QueueEntry.position)).where(QueueEntry.direction_id == direction_id).scalar()
    return (q or 0) + 1


def enqueue_driver_end(direction: Direction, driver: DriverProfile) -> QueueEntry:
    """Append driver to FIFO tail for direction (always at the end)."""
    now = datetime.now(timezone.utc)
    QueueEntry.delete().where(
        (QueueEntry.direction_id == direction.id) & (QueueEntry.driver_id == driver.id)
    ).execute()
    pos = _next_position(direction.id)
    return QueueEntry.create(direction=direction, driver=driver, position=pos, enqueued_at=now)


def remove_from_queue(direction: Direction, driver: DriverProfile) -> None:
    QueueEntry.delete().where(
        (QueueEntry.direction_id == direction.id) & (QueueEntry.driver_id == driver.id)
    ).execute()
    _normalize_positions(direction.id)


def _normalize_positions(direction_id: int) -> None:
    rows: List[QueueEntry] = list(
        QueueEntry.select()
        .where(QueueEntry.direction_id == direction_id)
        .order_by(QueueEntry.position, QueueEntry.enqueued_at)
    )
    for i, row in enumerate(rows, start=1):
        if row.position != i:
            QueueEntry.update(position=i).where(QueueEntry.id == row.id).execute()


def reorder_queue(direction_id: int, driver_ids_in_order: List[int], *, actor_telegram_id: int) -> None:
    now = datetime.now(timezone.utc)
    for pos, did in enumerate(driver_ids_in_order, start=1):
        QueueEntry.update(position=pos, enqueued_at=now).where(
            (QueueEntry.direction_id == direction_id) & (QueueEntry.driver_id == did)
        ).execute()
    audit_service.log_action(
        "queue_reorder",
        actor_telegram_id=actor_telegram_id,
        entity_type="direction",
        entity_id=str(direction_id),
        payload={"driver_ids": driver_ids_in_order},
    )


def fifo_first_online(direction: Direction) -> Optional[DriverProfile]:
    q = (
        QueueEntry.select(QueueEntry, DriverProfile)
        .join(DriverProfile, on=(QueueEntry.driver_id == DriverProfile.id))
        .where(
            (QueueEntry.direction_id == direction.id)
            & (DriverProfile.online == True)  # noqa: E712
            & (DriverProfile.status == "active")
        )
        .order_by(QueueEntry.position, QueueEntry.enqueued_at)
    )
    for row in q:
        return row.driver
    return None

"""Broadcast loading status updates to drivers and passengers."""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from aiogram import Bot

from app.models import Direction, DriverProfile, QueueEntry
from app.services import loading_service, order_service, queue_service
from app.services import queue_eta_service
from app.bot import messages

logger = logging.getLogger("taxi_bot.loading_notify")


def _notify_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


async def broadcast_loading_update(
    bot: Bot,
    direction_id: int,
    *,
    trigger_order_id: Optional[int] = None,
    loading_session_id: Optional[str] = None,
) -> None:
    direction = Direction.get_by_id(direction_id)

    for snap in loading_service.drivers_loading_on_direction(direction_id):
        drv = DriverProfile.get_by_id(snap.driver_id)
        if not getattr(drv, "loading_photos_ok_at", None) and snap.occupied_seats > 0:
            continue
        if not drv.loading:
            continue
        lines = [f"• #{p.order_id}: {p.seats} мест, {p.from_location}" for p in snap.passengers]
        pax_block = "\n".join(lines) if lines else "—"
        text = messages.format_driver_loading_status(
            route=f"{direction.from_label} → {direction.to_label}",
            status_label=snap.status_label,
            occupied=snap.occupied_seats,
            max_seats=snap.max_seats,
            passengers_block=pax_block,
        )
        try:
            await bot.send_message(drv.user.telegram_id, text)
        except Exception as e:
            logger.warning("loading notify driver %s: %s", drv.id, e)

    qe = (
        QueueEntry.select()
        .where(QueueEntry.direction_id == direction_id)
        .order_by(QueueEntry.position)
        .first()
    )
    if not qe:
        return
    nxt = DriverProfile.get_by_id(qe.driver_id)
    if not nxt.online or nxt.status != "active":
        return
    loading_drv = loading_service.drivers_loading_on_direction(direction_id)
    loader_name = loading_drv[0].full_name if loading_drv else "Водитель"
    slot = queue_eta_service.eta_for_driver(direction_id, nxt.id)
    from app.services.admin_notify import notify_driver_loading

    text = messages.format_queue_driver_loading_notice(
        loader_name=loader_name or "Водитель",
        route=f"{direction.from_label} → {direction.to_label}",
        position=qe.position,
        loading_label=slot.label if slot else None,
    )
    h = _notify_hash(text)
    if getattr(qe, "last_loading_notify_hash", None) == h:
        return
    QueueEntry.update(last_loading_notify_hash=h).where(QueueEntry.id == qe.id).execute()
    await notify_driver_loading(
        bot,
        nxt.user.telegram_id,
        loader_name or "Водитель",
        f"{direction.from_label} → {direction.to_label}",
        qe.position,
        loading_label=slot.label if slot else None,
        custom_text=text,
    )

"""Background tasks: loading reminders, underfill digest."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot

from app.config import get_settings
from app.models import DriverProfile, QueueEntry, Order, OrderStatus
from app.services import queue_eta_service
from app.services.admin_notify import notify_queue_underfill
from app.services.loading_service import direction_waiting_pool

logger = logging.getLogger("taxi_bot.scheduler")


async def scheduled_orders_activation_loop(bot: Bot, stop_event: asyncio.Event) -> None:
    from app.services import scheduled_trip_service

    while not stop_event.is_set():
        try:
            n = scheduled_trip_service.activate_due_orders()
            if n:
                logger.info("Activated %s scheduled orders for live queue", n)
        except Exception:
            logger.exception("scheduled orders activation tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=600)
        except asyncio.TimeoutError:
            pass


async def loading_reminder_loop(bot: Bot, stop_event: asyncio.Event) -> None:
    settings = get_settings()
    window = settings.loading_reminder_minutes_before
    while not stop_event.is_set():
        try:
            await _run_loading_reminders(bot, window)
        except Exception:
            logger.exception("loading reminder tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass


async def _run_loading_reminders(bot: Bot, window_min: int) -> None:
    now = datetime.now(timezone.utc)
    for qe in QueueEntry.select():
        if getattr(qe, "loading_reminder_sent_at", None):
            continue
        drv = DriverProfile.get_by_id(qe.driver_id)
        if not drv.online:
            continue
        slot = queue_eta_service.eta_for_driver(qe.direction_id, qe.driver_id)
        if not slot or slot.is_now:
            continue
        mins = slot.minutes_until
        if not (window_min - 5 <= mins <= window_min + 5):
            continue
        from app.models import Direction

        d = Direction.get_by_id(qe.direction_id)
        await notify_driver_loading_reminder(
            bot,
            drv.user.telegram_id,
            route=f"{d.from_label} → {d.to_label}",
            minutes=mins,
            label=slot.label,
        )
        QueueEntry.update(loading_reminder_sent_at=now).where(QueueEntry.id == qe.id).execute()


async def notify_driver_loading_reminder(
    bot: Bot,
    telegram_id: int,
    *,
    route: str,
    minutes: int,
    label: str,
) -> None:
    try:
        await bot.send_message(
            telegram_id,
            f"⏰ Через ~{minutes} мин ваша загрузка по маршруту {route}.\n"
            f"{label}\n"
            "Подготовьте машину и выезжайте на точку подачи.",
        )
    except Exception as e:
        logger.warning("loading reminder %s: %s", telegram_id, e)


async def check_underfill_on_direction(bot: Bot, direction_id: int) -> None:
    from app.models import Direction

    pool = direction_waiting_pool(direction_id)
    settings = get_settings()
    if pool["order_count"] < settings.queue_underfill_notify_min_orders:
        return
    d = Direction.get_by_id(direction_id)
    await notify_queue_underfill(
        bot,
        d.from_label,
        d.to_label,
        order_count=pool["order_count"],
        total_seats=pool["total_seats"],
    )


async def passenger_trip_reminder_loop(bot: Bot, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await _run_passenger_trip_reminders(bot)
        except Exception:
            logger.exception("passenger reminders tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass


async def _run_passenger_trip_reminders(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    rows = (
        Order.select()
        .where(
            (Order.scheduled_trip_id.is_null(False))
            & (Order.status.in_([
                OrderStatus.NEW.value,
                OrderStatus.ASSIGNED.value,
                OrderStatus.IN_PROGRESS.value,
                OrderStatus.AWAITING_PAYMENT.value,
            ]))
        )
        .limit(500)
    )
    from app.models.scheduled_trip import ScheduledTrip
    from app.models.direction import Direction

    for o in rows:
        try:
            trip = ScheduledTrip.get_by_id(o.scheduled_trip_id)
        except Exception:
            continue
        dep = trip.departure_at
        if dep.tzinfo is None:
            dep = dep.replace(tzinfo=timezone.utc)
        mins = int((dep - now).total_seconds() // 60)
        if mins < 0:
            continue
        if 1410 <= mins <= 1440 and not getattr(o, "reminder_24h_sent_at", None):
            await _send_trip_reminder(bot, o, dep, Direction.get_by_id(o.direction_id), "24 часа")
            Order.update(reminder_24h_sent_at=now).where(Order.id == o.id).execute()
            continue
        if 110 <= mins <= 120 and not getattr(o, "reminder_2h_sent_at", None):
            await _send_trip_reminder(bot, o, dep, Direction.get_by_id(o.direction_id), "2 часа")
            Order.update(reminder_2h_sent_at=now).where(Order.id == o.id).execute()
            continue
        if 25 <= mins <= 30 and not getattr(o, "reminder_30m_sent_at", None):
            await _send_trip_reminder(bot, o, dep, Direction.get_by_id(o.direction_id), "30 минут")
            Order.update(reminder_30m_sent_at=now).where(Order.id == o.id).execute()


async def _send_trip_reminder(bot: Bot, order: Order, dep: datetime, direction: Direction, left_label: str) -> None:
    from app.util.time_format import format_datetime_display

    text = (
        f"⏰ Напоминание о поездке #{order.id}\n"
        f"Маршрут: {direction.from_label} → {direction.to_label}\n"
        f"Выезд: {format_datetime_display(dep)}\n"
        f"До поездки примерно {left_label}."
    )
    try:
        await bot.send_message(order.passenger.telegram_id, text)
    except Exception as e:
        logger.warning("passenger reminder order=%s: %s", order.id, e)

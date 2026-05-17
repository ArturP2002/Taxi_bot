"""Relay messages between users and admins via Telegram."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot

from app.config import get_settings
from app.models import User, DriverProfile, Order, OrderStatus


async def relay_to_admins(
    bot: Bot,
    text: str,
    *,
    from_telegram_id: int,
    role: str,
    order_id: Optional[int] = None,
    driver_id: Optional[int] = None,
) -> None:
    header = f"📩 Сообщение от {role} (TG {from_telegram_id})"
    if order_id:
        header += f"\nЗаказ #{order_id}"
    if driver_id:
        header += f"\nВодитель #{driver_id}"
    full = f"{header}\n\n{text}"
    settings = get_settings()
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, full)
        except Exception:
            pass


async def relay_admin_reply(
    bot: Bot,
    admin_telegram_id: int,
    target_telegram_id: int,
    text: str,
) -> bool:
    if admin_telegram_id not in get_settings().admin_ids:
        return False
    try:
        await bot.send_message(target_telegram_id, f"📩 Ответ администратора:\n{text}")
        return True
    except Exception:
        return False


def active_order_for_passenger(user: User) -> Optional[Order]:
    statuses = [
        OrderStatus.NEW.value,
        OrderStatus.AWAITING_PAYMENT.value,
        OrderStatus.ASSIGNED.value,
        OrderStatus.IN_PROGRESS.value,
        OrderStatus.ADMIN_REVIEW.value,
    ]
    return (
        Order.select()
        .where((Order.passenger_id == user.id) & (Order.status.in_(statuses)))
        .order_by(Order.id.desc())
        .first()
    )


def active_driver_profile(user: User) -> Optional[DriverProfile]:
    try:
        return DriverProfile.get(user=user)
    except DriverProfile.DoesNotExist:
        return None

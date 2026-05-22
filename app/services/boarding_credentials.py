"""Send passenger boarding code + QR; recover code from DB for resend."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.models import Direction, Order, OrderStatus
from app.services import code_service

logger = logging.getLogger("taxi_bot.boarding_credentials")

_bot_username_cache: Optional[str] = None


async def get_bot_username(bot: Bot) -> str:
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        me = await bot.get_me()
        _bot_username_cache = (me.username or "").strip()
    except Exception as e:
        logger.warning("get_me failed: %s", e)
        _bot_username_cache = ""
    return _bot_username_cache


def boarding_code_for_order(order: Order) -> Optional[str]:
    raw = getattr(order, "boarding_code", None) or ""
    return code_service.normalize_boarding_code(str(raw)) if raw else None


def format_code_message(order: Order, direction: Direction, code: str) -> str:
    return (
        f"🔐 КОД ПОСАДКИ · заказ #{order.id}\n"
        f"📍 {direction.from_label} → {direction.to_label}\n\n"
        f"      {code}\n\n"
        "Назовите эти 6 цифр водителю или покажите QR ниже.\n"
        "Код действует до начала поездки."
    )


async def send_passenger_boarding_credentials(
    bot: Bot,
    order: Order,
    *,
    code: Optional[str] = None,
    direction: Optional[Direction] = None,
) -> bool:
    """
    Send prominent 6-digit code + scannable QR.
    Returns False if code unknown (legacy order without boarding_code).
    """
    order = Order.get_by_id(order.id)
    if order.status in (
        OrderStatus.COMPLETED.value,
        OrderStatus.CANCELLED.value,
        OrderStatus.IN_PROGRESS.value,
    ):
        return False
    if order.code_consumed_at:
        return False

    code = code or boarding_code_for_order(order)
    if not code:
        return False

    direction = direction or Direction.get_by_id(order.direction_id)
    username = await get_bot_username(bot)
    qr_payload = code_service.build_telegram_deeplink(username, order.id, code)
    png = code_service.render_qr_png(qr_payload)

    await bot.send_message(
        order.passenger.telegram_id,
        format_code_message(order, direction, code),
    )
    caption = (
        f"QR для заказа #{order.id}\n"
        "Водитель может отсканировать камерой (откроется бот) "
        "или вы сфотографируете QR для «📷 Сканировать QR»."
    )
    await bot.send_photo(
        order.passenger.telegram_id,
        BufferedInputFile(png, filename=f"order_{order.id}_qr.png"),
        caption=caption,
    )
    return True


async def notify_passenger_driver_assigned(
    bot: Bot,
    order: Order,
    driver,
    direction: Direction,
) -> None:
    from app.bot import messages as bot_messages

    try:
        await bot.send_message(
            order.passenger.telegram_id,
            bot_messages.format_order_summary(
                order,
                direction,
                driver_name=driver.full_name,
                extra=bot_messages.PASSENGER_BOARDING_CHECKLIST,
            ),
        )
    except Exception:
        pass
    try:
        await send_passenger_boarding_credentials(bot, order, direction=direction)
    except Exception as e:
        logger.warning("boarding credentials for order %s: %s", order.id, e)


async def issue_and_send_new_credentials(bot: Bot, order: Order) -> str:
    """Regenerate code (invalidates old QR) and send to passenger."""
    code = code_service.generate_six_digit_code()
    code_service.persist_boarding_code(order.id, code)
    await send_passenger_boarding_credentials(bot, Order.get_by_id(order.id), code=code)
    return code

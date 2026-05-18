"""Send notifications to all admins with a Mini App button."""
import logging
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings

logger = logging.getLogger("taxi_bot.admin_notify")


def _admin_keyboard() -> InlineKeyboardMarkup:
    settings = get_settings()
    ib = InlineKeyboardBuilder()
    if settings.mini_app_url:
        ib.button(text="📋 Открыть админку", web_app=WebAppInfo(url=settings.mini_app_url))
    ib.adjust(1)
    return ib.as_markup()


async def notify_admins(bot: Bot, text: str, extra_kb: Optional[InlineKeyboardMarkup] = None) -> None:
    settings = get_settings()
    kb = extra_kb or _admin_keyboard()
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)


async def notify_new_order(bot: Bot, order_id: int, direction_from: str, direction_to: str,
                           from_loc: str, to_loc: str, seats: int,
                           suggested_driver_name: str | None = None,
                           assignment_id: int | None = None) -> None:
    from app.bot.keyboards import admin_suggestion_inline

    text = (
        f"🆕 Новый заказ #{order_id}\n"
        f"📍 {direction_from} → {direction_to}\n"
        f"Откуда: {from_loc}\n"
        f"Куда: {to_loc}\n"
        f"Мест: {seats}\n"
    )
    if suggested_driver_name and assignment_id:
        text += f"\n🚗 Система предлагает: {suggested_driver_name}\n"
        text += "Подтвердите или выберите другого."
        await notify_admins(bot, text, extra_kb=admin_suggestion_inline(assignment_id))
    else:
        text += "\nНет подходящих водителей. Назначьте вручную в админке."
        await notify_admins(bot, text)


async def notify_suggestion_update(bot: Bot, order_id: int,
                                    suggested_driver_name: str | None = None,
                                    assignment_id: int | None = None) -> None:
    from app.bot.keyboards import admin_suggestion_inline

    if suggested_driver_name and assignment_id:
        text = (
            f"🔄 Заказ #{order_id}\n"
            f"Новое предложение: {suggested_driver_name}\n"
            "Подтвердите или выберите другого."
        )
        await notify_admins(bot, text, extra_kb=admin_suggestion_inline(assignment_id))
    else:
        text = (
            f"⚠️ Заказ #{order_id}\n"
            "Больше нет подходящих водителей. Назначьте вручную в админке."
        )
        await notify_admins(bot, text)


async def notify_driver_registered(
    bot: Bot,
    driver_name: str,
    telegram_id: int,
    *,
    route: str | None = None,
    max_seats: int | None = None,
    tariff: str | None = None,
) -> None:
    text = (
        f"👤 Новая заявка от водителя\n"
        f"Имя: {driver_name}\n"
        f"TG ID: {telegram_id}\n"
    )
    if route:
        text += f"Маршрут: {route}\n"
    if max_seats is not None:
        text += f"Мест: {max_seats}\n"
    if tariff:
        text += f"Тариф: {tariff}\n"
    text += "\nПодтвердите в админке."
    await notify_admins(bot, text)


async def notify_proposal(
    bot: Bot, from_label: str, to_label: str, driver_name: str, *, paired: bool = False
) -> None:
    text = (
        f"🗺 Предложен маршрут\n"
        f"{from_label} → {to_label}\n"
    )
    if paired:
        text += f"↩ Обратно: {to_label} → {from_label}\n"
    text += f"Водитель: {driver_name}\n\nРассмотрите в админке (пара туда/обратно)."
    await notify_admins(bot, text)


async def notify_payment_received(bot: Bot, driver_name: str, amount, payment_id: int) -> None:
    text = (
        f"💰 Платёж создан\n"
        f"Водитель: {driver_name}\n"
        f"Сумма: {amount} ₽\n"
        f"ID платежа: #{payment_id}\n\n"
        "Проверьте статус в админке."
    )
    await notify_admins(bot, text)


async def notify_driver_declined(bot: Bot, order_id: int, driver_name: str) -> None:
    text = (
        f"❌ Водитель отказался от заказа #{order_id}\n"
        f"Водитель: {driver_name}\n\n"
        "Назначьте другого в админке."
    )
    await notify_admins(bot, text)


async def notify_driver_approved_welcome(bot: Bot, telegram_id: int) -> None:
    from app.bot.messages import DRIVER_WELCOME_AFTER_APPROVAL

    try:
        await bot.send_message(telegram_id, DRIVER_WELCOME_AFTER_APPROVAL)
    except Exception as e:
        logger.warning("Failed to send driver welcome %s: %s", telegram_id, e)


async def notify_proposal_decision(
    bot: Bot, telegram_id: int, *, approved: bool, route: str, queue_position: int | None = None
) -> None:
    if approved:
        msg = f"✅ Маршрут одобрен: {route}"
        if queue_position:
            msg += f"\nВы №{queue_position} в очереди на этом направлении."
    else:
        msg = f"❌ Заявка на маршрут отклонена: {route}"
    try:
        await bot.send_message(telegram_id, msg)
    except Exception as e:
        logger.warning("Failed to notify driver %s: %s", telegram_id, e)


async def notify_driver_loading(
    bot: Bot,
    telegram_id: int,
    driver_name: str,
    route: str,
    position: int,
    *,
    loading_label: str | None = None,
) -> None:
    text = (
        f"ℹ️ Водитель {driver_name} на загрузке по маршруту {route}.\n"
        f"Вы №{position} в очереди."
    )
    if loading_label:
        text += f"\n⏱ Ваша загрузка: {loading_label}"
    else:
        text += " Ожидайте."
    try:
        await bot.send_message(telegram_id, text)
    except Exception as e:
        logger.warning("Failed to notify queue driver %s: %s", telegram_id, e)


async def notify_trip_started(
    bot: Bot,
    order_id: int,
    driver_name: str,
    *,
    route: str,
    seats: int,
    car_info: str | None,
    own_seats: int,
) -> None:
    car = car_info or "—"
    text = (
        f"▶️ Поездка #{order_id} началась\n"
        f"Водитель: {driver_name}\n"
        f"Маршрут: {route}\n"
        f"Мест в заказе: {seats}\n"
        f"Свои места: {own_seats}\n"
        f"Авто: {car}"
    )
    await notify_admins(bot, text)


async def notify_driver_action(bot: Bot, text: str) -> None:
    await notify_admins(bot, text)


async def notify_trip_completed(bot: Bot, order_id: int, driver_name: str, commission) -> None:
    text = (
        f"✅ Поездка #{order_id} завершена\n"
        f"Водитель: {driver_name}\n"
        f"Комиссия: {commission} ₽"
    )
    await notify_admins(bot, text)

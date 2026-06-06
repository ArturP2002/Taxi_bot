"""Deep-link and shared boarding verification handlers."""
from __future__ import annotations

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards
from app.models import (
    Direction,
    DriverProfile,
    Order,
    OrderStatus,
    User,
    UserRole,
)
from app.services import order_service, code_service


async def try_verify_from_deeplink(
    message: Message,
    state: FSMContext,
    bot: Bot,
    payload: str,
) -> bool:
    """
    Handle /start vc_{order}_{code} from QR scan.
    Returns True if payload was handled.
    """
    parsed = code_service.parse_verification_raw(payload)
    if not parsed or not parsed.code:
        return False

    from app.bot.users import ensure_user

    ensure_user(message.from_user)
    u = User.get(telegram_id=message.from_user.id)

    try:
        order = Order.get_by_id(parsed.order_id)
    except Order.DoesNotExist:
        await message.answer("Заказ не найден.")
        return True

    direction = Direction.get_by_id(order.direction_id)

    if u.role == UserRole.DRIVER.value:
        try:
            dprof = DriverProfile.get(user=u)
        except DriverProfile.DoesNotExist:
            await message.answer("Профиль водителя не найден.")
            return True

        if order.status == OrderStatus.IN_PROGRESS.value:
            await message.answer(
                f"Рейс по заказу #{order.id} уже в пути.",
                reply_markup=keyboards.trip_actions_kb(),
            )
            return True

        ok, key = order_service.verify_passenger_boarding(
            order, payload, driver_id=dprof.id, expected_order_id=order.id
        )
        if not ok:
            await message.answer(code_service.verification_error_label(key))
            return True

        from app.bot.handlers.driver import _after_passenger_boarded

        await state.clear()
        await _after_passenger_boarded(
            message, state, bot, dprof=dprof, order=Order.get_by_id(order.id)
        )
        return True

    if order.passenger_id == u.id:
        await message.answer(
            f"Это ваш заказ #{order.id}.\n"
            f"Код посадки: {parsed.code}\n"
            f"{direction.from_label} → {direction.to_label}\n\n"
            "Покажите QR или назовите код водителю при посадке.\n"
            "Повторно получить билет — кнопка «🔐 Код и QR» в меню.",
        )
        return True

    await message.answer(
        f"Заказ #{order.id}. Для посадки нужен водитель, назначенный в системе."
    )
    return True

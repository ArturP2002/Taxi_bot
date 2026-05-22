"""Deep-link and shared boarding verification handlers."""
from __future__ import annotations

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards
from app.models import (
    CommissionLedger,
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    AssignmentStatus,
    OrderStatus,
    User,
    UserRole,
)
from app.services import order_service, code_service
from app.services.boarding_credentials import send_passenger_boarding_credentials


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

        ass = order_service.get_accepted_assignment(order)
        if not ass or ass.driver_id != dprof.id:
            await message.answer(
                f"Заказ #{order.id} не назначен вам.\n"
                "Откройте «📥 Мой заказ» и «▶️ Старт поездки»."
            )
            return True

        if order.status == OrderStatus.IN_PROGRESS.value:
            await message.answer(
                f"Поездка #{order.id} уже начата.",
                reply_markup=keyboards.trip_actions_kb(),
            )
            return True

        if order.status != OrderStatus.ASSIGNED.value:
            await message.answer(
                f"Заказ #{order.id} в статусе «{order.status}» — старт недоступен."
            )
            return True

        ok, key = order_service.verify_order_code(
            order, payload, expected_order_id=order.id
        )
        if not ok:
            await message.answer(code_service.verification_error_label(key))
            return True

        await state.clear()
        dprof = DriverProfile.get_by_id(dprof.id)
        comm = CommissionLedger.select().where(CommissionLedger.order_id == order.id).first()
        comm_txt = f" Начислена комиссия: {comm.amount} ₽." if comm else ""
        await message.answer(
            f"✅ По QR: поездка #{order.id} началась.{comm_txt}\n"
            f"{direction.from_label} → {direction.to_label}",
            reply_markup=keyboards.trip_actions_kb(),
        )
        try:
            await bot.send_message(
                order.passenger.telegram_id,
                "Водитель отсканировал QR. Приятной поездки!",
            )
        except Exception:
            pass
        from app.services.admin_notify import notify_trip_started

        await notify_trip_started(
            bot,
            order.id,
            dprof.full_name or f"ID:{dprof.id}",
            route=f"{direction.from_label} → {direction.to_label}",
            seats=order.seats,
            car_info=dprof.car_info,
            own_seats=int(dprof.own_seats_reserved or 0),
        )
        return True

    if order.passenger_id == u.id:
        await message.answer(
            f"Это ваш заказ #{order.id}.\n"
            f"Код посадки: {parsed.code}\n"
            f"{direction.from_label} → {direction.to_label}\n\n"
            "Покажите QR или назовите код водителю при посадке.",
        )
        await send_passenger_boarding_credentials(bot, order, code=parsed.code, direction=direction)
        return True

    await message.answer(
        f"Заказ #{order.id}. Для посадки нужен водитель, назначенный в системе."
    )
    return True

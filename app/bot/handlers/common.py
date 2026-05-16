import time

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import get_settings
from app.bot import keyboards
from app.bot.users import ensure_user, is_admin

router = Router(name="common")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    ensure_user(message.from_user)
    await message.answer(
        "Междугороднее такси. Выберите действие:",
        reply_markup=keyboards.main_passenger_kb(),
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к панели администратора.")
        return
    settings = get_settings()
    base = settings.mini_app_url or f"{settings.base_url.rstrip('/')}/admin/"
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}v={int(time.time())}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть админку", web_app=WebAppInfo(url=url))]
    ])
    await message.answer("Панель администратора:", reply_markup=kb)


@router.message(F.text == "👤 Режим пассажира")
@router.message(Command("passenger"))
async def to_passenger(message: Message) -> None:
    ensure_user(message.from_user)
    await message.answer("Режим пассажира", reply_markup=keyboards.main_passenger_kb())


@router.message(F.text == "🧑‍✈️ Я водитель")
@router.message(Command("driver"))
async def im_driver(message: Message) -> None:
    ensure_user(message.from_user, prefer_driver=True)
    await message.answer("Меню водителя", reply_markup=keyboards.main_driver_kb())


@router.callback_query(F.data.startswith("adm_ok:"))
async def admin_confirm_suggestion(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    from app.models import (
        OrderDriverAssignment, AssignmentStatus, Order, Direction, DriverProfile,
    )
    from app.services import order_service

    aid = int(cb.data.split(":")[1])
    try:
        ass = OrderDriverAssignment.get_by_id(aid)
    except OrderDriverAssignment.DoesNotExist:
        await cb.answer("Назначение не найдено", show_alert=True)
        return

    if ass.status != AssignmentStatus.SUGGESTED.value:
        await cb.answer("Предложение уже обработано", show_alert=True)
        return

    try:
        confirmed = order_service.confirm_suggestion(
            ass, actor_telegram_id=cb.from_user.id,
        )
    except ValueError as e:
        reason = str(e)
        if reason == "driver_offline":
            await cb.answer("Водитель ушёл оффлайн", show_alert=True)
        elif reason == "capacity_exceeded":
            await cb.answer("У водителя нет свободных мест", show_alert=True)
        else:
            await cb.answer(f"Ошибка: {reason}", show_alert=True)
        return

    order = Order.get_by_id(ass.order_id)
    driver = DriverProfile.get_by_id(ass.driver_id)
    d = Direction.get_by_id(order.direction_id)

    text = (
        f"Вам назначен заказ #{order.id}\n"
        f"{d.from_label} → {d.to_label}\n"
        f"Откуда: {order.from_location}\n"
        f"Куда: {order.to_location}\n"
        f"Мест: {order.seats}\n"
        f"Подача: {order.pickup_location or '—'} {order.pickup_time_text or ''}\n"
        "Откройте «Мой заказ»."
    )
    try:
        await bot.send_message(
            driver.user.telegram_id, text,
            reply_markup=keyboards.assignment_inline(confirmed.id),
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            order.passenger.telegram_id,
            f"Водитель назначен по заказу #{order.id}.",
        )
    except Exception:
        pass

    await cb.message.edit_text(
        cb.message.text + f"\n\n✅ Подтверждено — назначен {driver.full_name or 'водитель'}",
        reply_markup=None,
    )
    await cb.answer("Водитель назначен")


@router.callback_query(F.data.startswith("adm_no:"))
async def admin_reject_suggestion(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    from app.models import (
        OrderDriverAssignment, AssignmentStatus, Order, DriverProfile,
    )
    from app.services import order_service
    from app.services.admin_notify import notify_suggestion_update

    aid = int(cb.data.split(":")[1])
    try:
        ass = OrderDriverAssignment.get_by_id(aid)
    except OrderDriverAssignment.DoesNotExist:
        await cb.answer("Назначение не найдено", show_alert=True)
        return

    if ass.status != AssignmentStatus.SUGGESTED.value:
        await cb.answer("Предложение уже обработано", show_alert=True)
        return

    next_ass = order_service.reject_suggestion(
        ass, actor_telegram_id=cb.from_user.id,
    )

    order = Order.get_by_id(ass.order_id)
    old_driver = DriverProfile.get_by_id(ass.driver_id)

    if next_ass:
        new_driver = DriverProfile.get_by_id(next_ass.driver_id)
        await cb.message.edit_text(
            cb.message.text + f"\n\n❌ {old_driver.full_name or 'Водитель'} отклонён",
            reply_markup=None,
        )
        await notify_suggestion_update(
            bot, order.id,
            suggested_driver_name=new_driver.full_name or f"ID:{new_driver.id}",
            assignment_id=next_ass.id,
        )
    else:
        await cb.message.edit_text(
            cb.message.text + f"\n\n❌ {old_driver.full_name or 'Водитель'} отклонён\n⚠️ Нет других водителей",
            reply_markup=None,
        )
        await notify_suggestion_update(bot, order.id)

    await cb.answer("Отклонено")

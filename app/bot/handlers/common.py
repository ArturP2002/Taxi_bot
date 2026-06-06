import time

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import get_settings
from app.bot import keyboards
from app.bot.messages import send_driver_rules
from app.bot.safe_callbacks import parse_callback_int
from app.bot.users import ensure_user, is_admin
from app.services import admin_relay

router = Router(name="common")


@router.message(F.text.contains("Заказать поездку"))
@router.message(Command("order"))
async def handle_order_ride(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user)
    from app.bot.handlers.passenger import continue_start_order

    await continue_start_order(message, state)


@router.message(F.text.contains("Я водитель"))
@router.message(Command("driver"))
async def handle_driver_mode(message: Message, state: FSMContext) -> None:
    from app.bot.handlers.driver import begin_driver_registration

    await begin_driver_registration(message, state)


@router.message(F.text.contains("Режим пассажира"))
@router.message(Command("passenger"))
async def handle_passenger_mode(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user)
    await message.answer(
        "Режим пассажира.",
        reply_markup=keyboards.main_passenger_kb(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    ensure_user(message.from_user)
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].lower().startswith("vc"):
        from app.bot.handlers.boarding import try_verify_from_deeplink

        if await try_verify_from_deeplink(message, state, bot, args[1]):
            return
    await state.clear()
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


@router.callback_query(F.data.startswith("adm_ok:"))
async def admin_confirm_suggestion(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    from app.models import (
        OrderDriverAssignment, AssignmentStatus, Order, Direction, DriverProfile,
    )
    from app.services import order_service

    aid = parse_callback_int(cb.data)
    if aid is None:
        await cb.answer("Некорректные данные", show_alert=True)
        return
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
        elif reason == "direction_mismatch":
            await cb.answer(
                "Водитель на другом направлении. Назначьте вручную в админке.",
                show_alert=True,
            )
        else:
            await cb.answer(f"Ошибка: {reason}", show_alert=True)
        return

    order = Order.get_by_id(ass.order_id)
    driver = DriverProfile.get_by_id(ass.driver_id)
    d = Direction.get_by_id(order.direction_id)

    from app.bot import messages as bot_messages

    text = (
        bot_messages.format_order_summary(order, d, extra="Откройте «Мой заказ».")
        + f"\nПодача: {order.pickup_location or '—'} {order.pickup_time_text or ''}"
    )
    try:
        await bot.send_message(
            driver.user.telegram_id, text,
            reply_markup=keyboards.assignment_inline(confirmed.id),
        )
    except Exception:
        pass

    try:
        from app.services.boarding_credentials import send_passenger_trip_ticket

        await send_passenger_trip_ticket(bot, order, driver=driver, direction=d)
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

    aid = parse_callback_int(cb.data)
    if aid is None:
        await cb.answer("Некорректные данные", show_alert=True)
        return
    try:
        ass = OrderDriverAssignment.get_by_id(aid)
    except OrderDriverAssignment.DoesNotExist:
        await cb.answer("Назначение не найдено", show_alert=True)
        return

    if ass.status != AssignmentStatus.SUGGESTED.value:
        await cb.answer("Предложение уже обработано", show_alert=True)
        return

    try:
            next_ass = order_service.reject_suggestion(
            ass, actor_telegram_id=cb.from_user.id,
        )
    except ValueError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return

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


@router.message(F.reply_to_message)
async def admin_reply_relay(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return
    replied = message.reply_to_message
    if not replied or not replied.text:
        return
    text = replied.text
    target_id = None
    if "TG " in text:
        for part in text.split():
            if part.startswith("TG") and part[2:].isdigit():
                target_id = int(part[2:])
                break
            if part.isdigit() and "TG" in text:
                try:
                    idx = text.find("TG")
                    chunk = text[idx:].split()[0].replace("TG", "").strip()
                    if chunk.isdigit():
                        target_id = int(chunk)
                except Exception:
                    pass
    if not target_id:
        import re
        m = re.search(r"TG\s*(\d+)", text)
        if m:
            target_id = int(m.group(1))
    if target_id:
        ok = await admin_relay.relay_admin_reply(bot, message.from_user.id, target_id, message.text)
        if ok:
            await message.answer("Ответ отправлен.")
        else:
            await message.answer("Не удалось отправить ответ.")

import io
from datetime import datetime, timezone

import qrcode
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from app.bot import keyboards
from app.bot.states import PassengerOrder, RelayChat, AdminRelayChat
from app.bot import messages as bot_messages
from app.bot.messages import send_passenger_rules
from app.bot.users import ensure_user
from app.config import get_settings
from app.models import Direction, Order, OrderStatus, PassengerPaymentStatus
from app.services import code_service, order_service
from app.services import direction_search
from app.services.admin_notify import notify_new_order
from app.services import passenger_payment_service
from app.services import admin_relay

router = Router(name="passenger")


async def _show_directions_page(message_or_cb, state: FSMContext, page: int = 0, mode: str = "browse") -> None:
    settings = get_settings()
    data = await state.get_data()
    search_flat = data.get("search_results") if mode == "search" else None
    chunk, page, pages = direction_search.get_groups_for_browse(
        search_results=search_flat,
        page=page,
        page_size=settings.direction_page_size,
    )
    if not chunk:
        text = "Направления не найдены."
        if hasattr(message_or_cb, "answer"):
            await message_or_cb.answer(text)
        else:
            await message_or_cb.message.answer(text)
        return
    kb = keyboards.direction_groups_inline(chunk, page=page, total_pages=pages, mode=mode)
    text = f"Выберите направление (стр. {page + 1}/{pages}). Пара ↩ — обратный рейс:"
    if hasattr(message_or_cb, "message"):
        await message_or_cb.message.edit_text(text, reply_markup=kb)
    else:
        await message_or_cb.answer(text, reply_markup=kb)


async def continue_start_order(message: Message, state: FSMContext) -> None:
    await send_passenger_rules(message)
    directions = direction_search.list_enabled_directions()
    if not directions:
        await message.answer("Направления пока недоступны.")
        return
    await state.set_state(PassengerOrder.choosing_direction)
    await state.update_data(search_results=None, dir_mode="browse")
    await _show_directions_page(message, state, 0, "browse")


@router.callback_query(PassengerOrder.choosing_direction, F.data == "dirsearch")
async def direction_search_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PassengerOrder.direction_search)
    await cb.message.answer("Введите город или маршрут для поиска:")
    await cb.answer()


@router.message(PassengerOrder.direction_search, F.text)
async def direction_search_query(message: Message, state: FSMContext) -> None:
    from app.services.direction_pairs import flatten_groups_for_search

    groups = direction_search.search_groups(message.text.strip())
    results = flatten_groups_for_search(groups)
    await state.set_state(PassengerOrder.choosing_direction)
    await state.update_data(search_results=results, dir_mode="search")
    if not results:
        await message.answer("Ничего не найдено. Попробуйте другой запрос или листайте все маршруты.")
        await state.update_data(search_results=None, dir_mode="browse")
        await _show_directions_page(message, state, 0, "browse")
        return
    await _show_directions_page(message, state, 0, "search")


@router.callback_query(PassengerOrder.choosing_direction, F.data.startswith("dirpage:"))
async def direction_page(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    page = int(parts[1])
    mode = parts[2] if len(parts) > 2 else "browse"
    await _show_directions_page(cb, state, page, mode)
    await cb.answer()


@router.callback_query(PassengerOrder.choosing_direction, F.data.startswith("dirpick:"))
async def pick_direction(cb: CallbackQuery, state: FSMContext) -> None:
    did = int(cb.data.split(":")[1])
    await state.update_data(direction_id=did)
    await state.set_state(PassengerOrder.from_location)
    await cb.message.answer("Точка отправления (текстом):", reply_markup=keyboards.cancel_kb())
    await cb.answer()


@router.message(PassengerOrder.from_location, F.text)
async def from_loc(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    await state.update_data(from_location=message.text.strip())
    await state.set_state(PassengerOrder.to_location)
    await message.answer("Точка назначения:")


@router.message(PassengerOrder.to_location, F.text)
async def to_loc(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    await state.update_data(to_location=message.text.strip())
    await state.set_state(PassengerOrder.seats)
    await message.answer("Количество мест:", reply_markup=keyboards.seats_kb())


@router.message(PassengerOrder.seats, F.text)
async def seats_pick(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    if message.text not in {str(i) for i in range(keyboards.SEATS_ORDER_MIN, keyboards.SEATS_ORDER_MAX + 1)}:
        await message.answer(f"Выберите {keyboards.SEATS_ORDER_MIN}–{keyboards.SEATS_ORDER_MAX}.")
        return
    await state.update_data(seats=int(message.text))
    await state.set_state(PassengerOrder.phone)
    await message.answer("Номер телефона:", reply_markup=keyboards.cancel_kb())


@router.message(PassengerOrder.phone, F.text)
async def phone_enter(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    data = await state.get_data()
    await state.clear()
    user = ensure_user(message.from_user)
    direction = Direction.get_by_id(data["direction_id"])
    code = code_service.generate_six_digit_code()
    now = datetime.now(timezone.utc)

    pay_status = PassengerPaymentStatus.NOT_REQUIRED.value
    order_status = OrderStatus.NEW.value
    if getattr(direction, "online_payment_required", False):
        pay_status = PassengerPaymentStatus.AWAITING.value
        order_status = OrderStatus.AWAITING_PAYMENT.value

    order = Order.create(
        direction=direction,
        passenger=user,
        from_location=data["from_location"],
        to_location=data["to_location"],
        seats=data["seats"],
        platform_seats=data["seats"],
        phone=message.text.strip(),
        status=order_status,
        passenger_payment_status=pay_status,
        confirmation_code_hash="tmp",
        code_issued_at=now,
    )
    Order.update(confirmation_code_hash=code_service.hash_code(order.id, code)).where(Order.id == order.id).execute()
    order = Order.get_by_id(order.id)
    token = code_service.build_qr_token(order.id)

    qr = qrcode.make(token)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)
    from app.services import loading_service

    pool = loading_service.direction_waiting_pool(direction.id)
    loading_cars = loading_service.drivers_loading_on_direction(direction.id)
    caption = (
        bot_messages.format_order_summary(order, direction, extra=f"Код: {code}")
        + "\n\n"
        "Назовите код водителю при посадке или покажите QR.\n"
        "«📞 Связь с водителем» — после назначения. «📞 Связь с админом» — в любой момент."
    )
    if loading_cars:
        lines = [c.status_label for c in loading_cars[:4]]
        caption += "\n\n🚐 Сейчас набирают: " + "; ".join(lines)
    if pool["order_count"]:
        caption += (
            f"\n📋 В очереди на рейс: {pool['total_seats']} мест "
            f"({pool['order_count']} заявок)."
        )
    from app.services import overflow_service

    if overflow_service.order_has_overflow(order):
        overflow_service.mark_order_overflow_review(order)
        order = Order.get_by_id(order.id)
        caption += f"\n\n{bot_messages.PASSENGER_OVERFLOW_MSG}"
    extra_kb = None
    if order_status == OrderStatus.AWAITING_PAYMENT.value:
        fare = passenger_payment_service.passenger_fare_amount(order)
        caption += f"\n\nОплата онлайн: {fare} ₽ (до подтверждения админом)."
        extra_kb = keyboards.passenger_pay_inline(order.id)

    await message.answer_photo(
        BufferedInputFile(buf.read(), filename="qr.png"),
        caption=caption,
        reply_markup=extra_kb or keyboards.main_passenger_kb(),
    )

    if order.status == OrderStatus.ADMIN_REVIEW.value:
        from app.services.admin_notify import notify_sos_overflow

        await notify_sos_overflow(
            bot,
            order.id,
            seats=order.seats,
            direction_from=direction.from_label,
            direction_to=direction.to_label,
            from_loc=order.from_location,
            to_loc=order.to_location,
        )
    elif order_status == OrderStatus.NEW.value:
        suggestion = order_service.suggest_driver_for_order(order)
        suggested_name = None
        assignment_id = None
        if suggestion:
            from app.models import DriverProfile
            drv = DriverProfile.get_by_id(suggestion.driver_id)
            suggested_name = drv.full_name or f"ID:{drv.id}"
            assignment_id = suggestion.id
        await notify_new_order(
            bot, order.id, direction.from_label, direction.to_label,
            order.from_location, order.to_location, order.seats,
            suggested_driver_name=suggested_name,
            assignment_id=assignment_id,
        )
    else:
        await notify_new_order(
            bot, order.id, direction.from_label, direction.to_label,
            order.from_location, order.to_location, order.seats,
            suggested_driver_name=None,
            assignment_id=None,
        )


@router.callback_query(F.data.startswith("pay:"))
async def pay_order(cb: CallbackQuery, bot: Bot) -> None:
    oid = int(cb.data.split(":")[1])
    order = Order.get_by_id(oid)
    result = passenger_payment_service.init_passenger_payment(order)
    if result.get("awaiting_admin"):
        await cb.message.answer(
            f"Заказ #{oid}: ожидает подтверждения оплаты администратором ({result['payment_id']})."
        )
    elif result.get("confirmation_url"):
        await cb.message.answer(
            f"Оплата заказа #{oid}: {passenger_payment_service.passenger_fare_amount(order)} ₽\n"
            f"{result['confirmation_url']}\n\nПосле оплаты нажмите «Проверить оплату»."
        )
    else:
        await cb.message.answer(f"Платёж создан. ID: {result.get('payment_id')}")
    await cb.answer()


@router.callback_query(F.data.startswith("paycheck:"))
async def pay_check(cb: CallbackQuery) -> None:
    oid = int(cb.data.split(":")[1])
    order = Order.get_by_id(oid)
    status = passenger_payment_service.check_passenger_payment(order)
    msgs = {
        "paid": "Оплата подтверждена. Заявка передана в обработку.",
        "pending": "Платёж ещё не прошёл.",
        "awaiting_admin": "Ожидает подтверждения администратором.",
        "failed": "Платёж отменён.",
        "no_payment": "Сначала нажмите «Оплатить онлайн».",
    }
    await cb.message.answer(msgs.get(status, status))
    await cb.answer()


def _contactable_order(user) -> Order | None:
    from app.models import User
    u = User.get(telegram_id=user.id)
    for st in (OrderStatus.ASSIGNED.value, OrderStatus.IN_PROGRESS.value):
        o = (
            Order.select()
            .where((Order.passenger_id == u.id) & (Order.status == st))
            .order_by(Order.id.desc())
            .first()
        )
        if o:
            return o
    return None


@router.message(F.text == "📞 Связь с водителем")
@router.message(F.text == "📞 Связь")
@router.message(Command("contact"))
async def contact_driver(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user)
    o = _contactable_order(message.from_user)
    if not o:
        await message.answer(
            "Нет заказа с назначенным водителем. Связь доступна после назначения."
        )
        return
    await state.set_state(RelayChat.active)
    await state.update_data(relay_order_id=o.id)
    await message.answer(
        f"Чат с водителем по заказу #{o.id}. /stop чтобы выйти."
    )


@router.message(F.text == "📞 Связь с админом")
async def contact_admin(message: Message, state: FSMContext) -> None:
    user = ensure_user(message.from_user)
    o = admin_relay.active_order_for_passenger(user)
    await state.set_state(AdminRelayChat.active)
    await state.update_data(admin_relay_order_id=o.id if o else None)
    hint = f" по заказу #{o.id}" if o else ""
    await message.answer(f"Чат с администратором{hint}. Пишите сообщение. /stop — выход.")


@router.message(AdminRelayChat.active, F.text)
async def relay_admin_passenger(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text.startswith("/stop"):
        await state.clear()
        await message.answer("Чат закрыт.", reply_markup=keyboards.main_passenger_kb())
        return
    data = await state.get_data()
    await admin_relay.relay_to_admins(
        bot,
        message.text,
        from_telegram_id=message.from_user.id,
        role="пассажир",
        order_id=data.get("admin_relay_order_id"),
    )
    await message.answer("Сообщение отправлено администратору.")


@router.message(RelayChat.active, F.text)
async def relay_passenger(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text.startswith("/stop"):
        await state.clear()
        await message.answer("Чат закрыт.", reply_markup=keyboards.main_passenger_kb())
        return
    data = await state.get_data()
    oid = data.get("relay_order_id")
    if not oid:
        await state.clear()
        return
    from app.models import OrderDriverAssignment, AssignmentStatus, DriverProfile

    ass = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == oid)
            & (OrderDriverAssignment.status.in_([
                AssignmentStatus.ACCEPTED.value,
                AssignmentStatus.PENDING.value,
            ]))
        )
        .first()
    )
    if not ass:
        await message.answer("Нет назначенного водителя.")
        return
    drv = DriverProfile.get_by_id(ass.driver_id)
    tid = drv.user.telegram_id
    text = f"💬 Заказ #{oid} (пассажир):\n{message.text}"
    try:
        await bot.send_message(tid, text)
    except Exception:
        await message.answer("Не удалось доставить сообщение.")
        return
    await message.answer("Отправлено.")


@router.message(RelayChat.active)
async def relay_non_text(message: Message) -> None:
    await message.answer("Пока поддерживаются только текстовые сообщения.")

from datetime import date, datetime, timezone

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from app.bot import keyboards
from app.bot.states import PassengerOrder
from app.bot import messages as bot_messages
from app.bot.messages import send_passenger_rules
from app.bot.users import ensure_user
from app.config import get_settings
from app.models import Direction, Order, OrderStatus, PassengerPaymentStatus
from app.services import code_service, order_service
from app.services import direction_search
from app.services.admin_notify import notify_new_order
from app.services import passenger_payment_service
from app.services import scheduled_trip_service
from app.services.boarding_credentials import (
    send_passenger_boarding_credentials,
    boarding_code_for_order,
)

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
    await state.update_data(direction_id=did, scheduled_trip_id=None)
    await state.set_state(PassengerOrder.choosing_trip_date)
    now = datetime.now(timezone.utc)
    avail = scheduled_trip_service.available_dates_for_direction(did)
    await cb.message.answer(
        "Выберите дату рейса или «Ближайший рейс»:",
        reply_markup=keyboards.trip_calendar_kb(
            now.year, now.month, available_dates=avail, direction_id=did
        ),
    )
    await cb.answer()


@router.callback_query(PassengerOrder.choosing_trip_date, F.data.startswith("tcal:"))
async def trip_calendar_cb(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    action = parts[1]
    if action == "noop":
        await cb.answer()
        return
    direction_id = int(parts[2])
    if action == "nav":
        ym = parts[3].split("-")
        year, month = int(ym[0]), int(ym[1])
        avail = scheduled_trip_service.available_dates_for_direction(direction_id)
        await cb.message.edit_reply_markup(
            reply_markup=keyboards.trip_calendar_kb(
                year, month, available_dates=avail, direction_id=direction_id
            )
        )
        await cb.answer()
        return
    if action == "asap":
        await state.update_data(scheduled_trip_id=None)
        await state.set_state(PassengerOrder.from_location)
        await cb.message.answer("Точка отправления (текстом):", reply_markup=keyboards.cancel_kb())
        await cb.answer()
        return
    if action == "day":
        day = date.fromisoformat(parts[3])
        trips = scheduled_trip_service.trips_on_date(direction_id, day)
        if not trips:
            await cb.answer("Нет рейсов на эту дату", show_alert=True)
            return
        if len(trips) == 1:
            await state.update_data(scheduled_trip_id=trips[0].id)
            await state.set_state(PassengerOrder.from_location)
            from app.util.time_format import format_datetime_display

            label = format_datetime_display(trips[0].departure_at)
            await cb.message.answer(
                f"Рейс: {label}\nТочка отправления (текстом):",
                reply_markup=keyboards.cancel_kb(),
            )
            await cb.answer()
            return
        await cb.message.answer(
            "Выберите время рейса:",
            reply_markup=keyboards.scheduled_trips_pick_kb(trips),
        )
        await cb.answer()
        return
    if action == "trip":
        trip_id = int(parts[2])
        await state.update_data(scheduled_trip_id=trip_id)
        await state.set_state(PassengerOrder.from_location)
        await cb.message.answer("Точка отправления (текстом):", reply_markup=keyboards.cancel_kb())
        await cb.answer()
        return
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

    trip_id = data.get("scheduled_trip_id")
    scheduled_activated = False
    trip_label = ""
    if trip_id:
        from app.models.scheduled_trip import ScheduledTrip

        try:
            trip = ScheduledTrip.get_by_id(int(trip_id))
            scheduled_trip_service.book_seats(int(trip_id), int(data["seats"]))
            from app.util.time_format import format_datetime_display

            trip_label = f"\n📅 Рейс: {format_datetime_display(trip.departure_at)}"
            scheduled_activated = scheduled_trip_service.trip_departure_day_reached(trip)
        except ValueError as e:
            await message.answer(f"Не удалось забронировать: {e}")
            return
        except Exception:
            await message.answer("Рейс недоступен. Выберите другое время.")
            return

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
        scheduled_trip_id=int(trip_id) if trip_id else None,
        scheduled_activated=scheduled_activated,
    )
    code_service.persist_boarding_code(order.id, code)
    order = Order.get_by_id(order.id)
    from app.services import loading_service

    pool = loading_service.direction_waiting_pool(direction.id)
    loading_cars = loading_service.drivers_loading_on_direction(direction.id)
    caption = (
        bot_messages.format_order_summary(order, direction)
        + trip_label
        + "\n\n"
        "Код и QR отправлены отдельными сообщениями ниже.\n"
        "«📞 Связь с водителем» — после назначения. «📞 Связь с админом» — в любой момент."
    )
    if trip_id and not scheduled_activated:
        caption += "\n⏳ Заказ в очереди на выбранную дату — водитель назначится ближе к рейсу."
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

    await message.answer(caption, reply_markup=extra_kb or keyboards.main_passenger_kb())
    await send_passenger_boarding_credentials(
        bot, order, code=code, direction=direction
    )

    if order.status == OrderStatus.ADMIN_REVIEW.value:
        from app.services.admin_notify import notify_sos_overflow
        from app.services import overflow_service

        cap = overflow_service.direction_capacity_info(order.direction_id)
        await notify_sos_overflow(
            bot,
            order.id,
            seats=order.seats,
            direction_from=direction.from_label,
            direction_to=direction.to_label,
            from_loc=order.from_location,
            to_loc=order.to_location,
            max_single_car_seats=cap.max_single_car_seats,
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


def _active_boarding_order(user) -> Order | None:
    from app.models import User

    u = User.get(telegram_id=user.id)
    for st in (
        OrderStatus.NEW.value,
        OrderStatus.ASSIGNED.value,
        OrderStatus.AWAITING_PAYMENT.value,
        OrderStatus.ADMIN_REVIEW.value,
    ):
        o = (
            Order.select()
            .where((Order.passenger_id == u.id) & (Order.status == st))
            .order_by(Order.id.desc())
            .first()
        )
        if o and not o.code_consumed_at:
            return o
    return None


@router.message(F.text == keyboards.BTN_BOARDING_CODE)
async def resend_boarding_code(message: Message, bot: Bot) -> None:
    ensure_user(message.from_user)
    o = _active_boarding_order(message.from_user)
    if not o:
        await message.answer(
            "Нет активного заказа. Сначала оформите поездку или дождитесь назначения."
        )
        return
    if not boarding_code_for_order(o):
        from app.services.boarding_credentials import issue_and_send_new_credentials

        await issue_and_send_new_credentials(bot, o)
        await message.answer("Сгенерирован новый код (старый QR больше не действует).")
        return
    ok = await send_passenger_boarding_credentials(bot, o)
    if ok:
        await message.answer(f"Код и QR для заказа #{o.id} отправлены выше.")
    else:
        await message.answer("Не удалось отправить код. Обратитесь к администратору.")


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
    await state.clear()
    ensure_user(message.from_user)
    o = _contactable_order(message.from_user)
    if not o:
        await message.answer(
            "Нет заказа с назначенным водителем. Связь доступна после назначения."
        )
        return
    from app.models import OrderDriverAssignment, AssignmentStatus, DriverProfile

    ass = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == o.id)
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
    await message.answer(
        f"Заказ #{o.id} — напишите водителю в личный чат:",
        reply_markup=keyboards.contact_user_inline(tid, "💬 Водитель"),
    )


@router.message(F.text == "📞 Связь с админом")
async def contact_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings()
    if not settings.admin_ids:
        await message.answer("Администратор не настроен.")
        return
    await message.answer(
        "Связь с администратором:",
        reply_markup=keyboards.contact_admins_inline(settings.admin_ids),
    )

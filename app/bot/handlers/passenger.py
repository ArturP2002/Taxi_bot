from datetime import datetime, timezone
import json

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from app.bot import keyboards
from app.bot.states import PassengerOrder, PassengerCabinet
from app.bot import messages as bot_messages
from app.bot.messages import send_passenger_rules
from app.bot.users import ensure_user
from app.config import get_settings
from app.models import (
    Direction,
    Order,
    OrderStatus,
    PassengerPaymentStatus,
    OrderChangeRequest,
    OrderChangeRequestStatus,
    User,
)
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

_PASSENGER_STEP_META = {
    "requested_departure_at": {
        "label": "дату и время выезда",
        "prev_state": PassengerOrder.requested_departure,
        "next_state": PassengerOrder.from_location,
        "next_prompt": "Точка отправления (текстом):",
        "next_markup": "cancel",
    },
    "from_location": {
        "label": "точку отправления",
        "prev_state": PassengerOrder.from_location,
        "next_state": PassengerOrder.to_location,
        "next_prompt": "Точка назначения:",
        "next_markup": "cancel",
    },
    "to_location": {
        "label": "точку назначения",
        "prev_state": PassengerOrder.to_location,
        "next_state": PassengerOrder.seats,
        "next_prompt": "Количество мест:",
        "next_markup": "seats",
    },
    "seats": {
        "label": "количество мест",
        "prev_state": PassengerOrder.seats,
        "next_state": PassengerOrder.phone,
        "next_prompt": "Номер телефона:",
        "next_markup": "cancel",
    },
    "phone": {
        "label": "номер телефона",
        "prev_state": PassengerOrder.phone,
        "next_state": PassengerOrder.confirm,
        "next_prompt": None,
        "next_markup": None,
    },
}


async def _enter_passenger_step_confirmation(
    message: Message,
    state: FSMContext,
    *,
    field: str,
    value,
    display: str,
) -> None:
    await state.update_data(pending_field=field, pending_value=value)
    await state.set_state(PassengerOrder.confirm_step)
    label = _PASSENGER_STEP_META[field]["label"]
    await message.answer(
        f"Подтвердите {label}: {display}",
        reply_markup=keyboards.confirm_edit_kb(),
    )


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


def _passenger_blocked(user) -> bool:
    from app.models import User

    try:
        u = User.get(telegram_id=user.id)
        return bool(getattr(u, "is_blocked", False))
    except Exception:
        return False


async def continue_start_order(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user)
    if _passenger_blocked(message.from_user):
        await message.answer(
            "Доступ ограничен. Обратитесь к администратору сервиса."
        )
        return
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
    hint = (
        "Выберите дату рейса (кнопки ниже) или «Как можно скорее» — без привязки к расписанию."
    )
    if not avail:
        hint += "\n\nНа ближайшие месяцы рейсов пока нет — администратор добавит их в календаре."
    await cb.message.answer(
        hint,
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
    if action == "asap":
        await state.update_data(scheduled_trip_id=None, requested_departure_at=None)
        await state.set_state(PassengerOrder.from_location)
        await cb.message.answer("Точка отправления (текстом):", reply_markup=keyboards.cancel_kb())
        await cb.answer()
        return
    if action == "custom":
        from app.util.time_format import DATETIME_DISPLAY_HINT

        await state.update_data(scheduled_trip_id=None)
        await state.set_state(PassengerOrder.requested_departure)
        await cb.message.answer(
            f"Укажите желаемую дату и время выезда.\nФормат: {DATETIME_DISPLAY_HINT}",
            reply_markup=keyboards.cancel_kb(),
        )
        await cb.answer()
        return
    if action in {"nav", "day", "trip"}:
        await cb.answer("Выбор дат через кнопки отключён. Используйте «Указать свою дату и время».", show_alert=True)
        return
    await cb.answer()


@router.message(PassengerOrder.requested_departure, F.text)
async def requested_departure_enter(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    from app.util.time_format import parse_datetime_display, DATETIME_DISPLAY_HINT
    from app.services import trip_request_service

    try:
        dep = parse_datetime_display(message.text.strip())
        trip_request_service.validate_requested_departure(dep)
    except ValueError as e:
        err = str(e)
        if err == "departure_in_past":
            await message.answer("Укажите дату и время в будущем.")
            return
        if err == "departure_too_far":
            settings = get_settings()
            await message.answer(
                f"Дата слишком далеко. Максимум на "
                f"{settings.scheduled_trip_booking_days_ahead} дней вперёд."
            )
            return
        await message.answer(f"Неверный формат. {DATETIME_DISPLAY_HINT}")
        return
    await state.update_data(scheduled_trip_id=None)
    from app.util.time_format import format_datetime_display
    await _enter_passenger_step_confirmation(
        message,
        state,
        field="requested_departure_at",
        value=dep.isoformat(),
        display=format_datetime_display(dep),
    )


@router.message(PassengerOrder.from_location, F.text)
async def from_loc(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    text = message.text.strip()
    await _enter_passenger_step_confirmation(
        message,
        state,
        field="from_location",
        value=text,
        display=text,
    )


@router.message(PassengerOrder.to_location, F.text)
async def to_loc(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    text = message.text.strip()
    await _enter_passenger_step_confirmation(
        message,
        state,
        field="to_location",
        value=text,
        display=text,
    )


@router.message(PassengerOrder.seats, F.text)
async def seats_pick(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    if message.text not in {str(i) for i in range(keyboards.SEATS_ORDER_MIN, keyboards.SEATS_ORDER_MAX + 1)}:
        await message.answer(f"Выберите {keyboards.SEATS_ORDER_MIN}–{keyboards.SEATS_ORDER_MAX}.")
        return
    seats = int(message.text)
    await _enter_passenger_step_confirmation(
        message,
        state,
        field="seats",
        value=seats,
        display=str(seats),
    )


@router.message(PassengerOrder.phone, F.text)
async def phone_enter(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    await _enter_passenger_step_confirmation(
        message,
        state,
        field="phone",
        value=message.text.strip(),
        display=message.text.strip(),
    )


@router.message(PassengerOrder.confirm_step, F.text)
async def passenger_confirm_step(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    data = await state.get_data()
    field = data.get("pending_field")
    if field not in _PASSENGER_STEP_META:
        await state.clear()
        await message.answer("Сессия подтверждения устарела.", reply_markup=keyboards.main_passenger_kb())
        return
    meta = _PASSENGER_STEP_META[field]
    if message.text == keyboards.BTN_BACK:
        await state.set_state(meta["prev_state"])
        current_val = data.get("pending_value")
        from app.util.time_format import format_datetime_display
        if field == "requested_departure_at" and current_val:
            try:
                dep = datetime.fromisoformat(str(current_val))
                if dep.tzinfo is None:
                    dep = dep.replace(tzinfo=timezone.utc)
                shown = format_datetime_display(dep)
            except Exception:
                shown = str(current_val)
        else:
            shown = str(current_val or "—")
        await message.answer(
            f"Введите значение заново.\nТекущее: {shown}",
            reply_markup=keyboards.cancel_kb(),
        )
        return
    if message.text != "✅ Подтвердить":
        await message.answer("Нажмите «✅ Подтвердить», «⬅️ Назад» или «❌ Отмена».")
        return

    await state.update_data(**{field: data.get("pending_value")}, pending_field=None, pending_value=None)
    if meta["next_state"] == PassengerOrder.confirm:
        data = await state.get_data()
        direction = Direction.get_by_id(data["direction_id"])
        from app.util.time_format import format_datetime_display

        req_txt = ""
        if data.get("requested_departure_at"):
            dep = datetime.fromisoformat(data["requested_departure_at"])
            if dep.tzinfo is None:
                dep = dep.replace(tzinfo=timezone.utc)
            req_txt = f"\nВыезд: {format_datetime_display(dep)}"
        await state.set_state(PassengerOrder.confirm)
        await message.answer(
            "Проверьте заявку перед отправкой:\n"
            f"Маршрут: {direction.from_label} → {direction.to_label}{req_txt}\n"
            f"Откуда: {data['from_location']}\n"
            f"Куда: {data['to_location']}\n"
            f"Мест: {data['seats']}\n"
            f"Телефон: {data['phone']}",
            reply_markup=keyboards.confirm_edit_kb(),
        )
        return

    await state.set_state(meta["next_state"])
    markup = None
    if meta["next_markup"] == "cancel":
        markup = keyboards.cancel_kb()
    elif meta["next_markup"] == "seats":
        markup = keyboards.seats_kb()
    await message.answer(meta["next_prompt"], reply_markup=markup)


@router.message(PassengerOrder.confirm, F.text)
async def passenger_confirm_submit(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    if message.text == keyboards.BTN_BACK:
        data = await state.get_data()
        await state.set_state(PassengerOrder.phone)
        await message.answer(
            f"Вернулись на шаг телефона.\nТекущее: {data.get('phone', '—')}\nВведите номер ещё раз:",
            reply_markup=keyboards.cancel_kb(),
        )
        return
    if message.text != "✅ Подтвердить":
        await message.answer("Нажмите «✅ Подтвердить», «⬅️ Назад» или «❌ Отмена».")
        return
    data = await state.get_data()
    await state.clear()
    user = ensure_user(message.from_user)
    if getattr(user, "is_blocked", False):
        await message.answer(
            "Доступ ограничен. Обратитесь к администратору сервиса.",
            reply_markup=keyboards.main_passenger_kb(),
        )
        return
    direction = Direction.get_by_id(data["direction_id"])
    now = datetime.now(timezone.utc)
    requested_iso = data.get("requested_departure_at")
    is_trip_request = bool(requested_iso) and not data.get("scheduled_trip_id")

    pay_status = PassengerPaymentStatus.NOT_REQUIRED.value
    order_status = OrderStatus.NEW.value
    if is_trip_request:
        order_status = OrderStatus.AWAITING_SCHEDULED_TRIP.value
    elif getattr(direction, "online_payment_required", False):
        pay_status = PassengerPaymentStatus.AWAITING.value
        order_status = OrderStatus.AWAITING_PAYMENT.value

    trip_id = data.get("scheduled_trip_id")
    requested_dep = None
    if requested_iso:
        requested_dep = datetime.fromisoformat(requested_iso)
        if requested_dep.tzinfo is None:
            requested_dep = requested_dep.replace(tzinfo=timezone.utc)
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
        phone=data["phone"].strip(),
        status=order_status,
        passenger_payment_status=pay_status,
        confirmation_code_hash="pending" if is_trip_request else "tmp",
        code_issued_at=None if is_trip_request else now,
        scheduled_trip_id=int(trip_id) if trip_id else None,
        scheduled_activated=scheduled_activated,
        requested_departure_at=requested_dep,
    )
    code = None
    if not is_trip_request:
        code = code_service.generate_six_digit_code()
        code_service.persist_boarding_code(order.id, code)
    order = Order.get_by_id(order.id)
    from app.services import loading_service

    pool = loading_service.direction_waiting_pool(direction.id)
    loading_cars = loading_service.drivers_loading_on_direction(direction.id)
    if is_trip_request:
        from app.util.time_format import format_datetime_display
        from app.services import trip_request_service
        from app.services.admin_notify import notify_trip_departure_request

        candidate_trip = trip_request_service.find_best_trip_for_request(
            direction_id=direction.id,
            requested_departure_at=requested_dep,
            seats=int(order.seats),
        )
        if candidate_trip is not None:
            try:
                await trip_request_service.fulfill_and_notify(
                    bot,
                    order.id,
                    candidate_trip,
                    actor_telegram_id=None,
                )
                await message.answer(
                    "✅ Найден подходящий рейс по вашему времени. Заказ автоматически подтверждён.",
                    reply_markup=keyboards.main_passenger_kb(),
                )
                return
            except ValueError:
                pass

        dep_label = format_datetime_display(requested_dep)
        caption = (
            f"✅ Заявка принята · #{order.id}\n"
            f"📍 {direction.from_label} → {direction.to_label}\n"
            f"🕐 Желаемый выезд: {dep_label}\n"
            f"Откуда: {order.from_location}\n"
            f"Куда: {order.to_location}\n"
            f"Мест: {order.seats}\n\n"
            "Администратор создаст рейс в календаре и привяжет ваш заказ.\n"
            "Код посадки и QR придут после подтверждения рейса.\n"
            "«📞 Связь с админом» — в любой момент."
        )
        await message.answer(caption, reply_markup=keyboards.main_passenger_kb())
        await notify_trip_departure_request(
            bot,
            order.id,
            direction_from=direction.from_label,
            direction_to=direction.to_label,
            departure_display=dep_label,
            from_loc=order.from_location,
            to_loc=order.to_location,
            seats=order.seats,
            phone=order.phone,
            passenger_label=trip_request_service.user_display_name(user),
        )
        return

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


async def _send_boarding_code_for_user(message: Message, bot: Bot) -> None:
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


@router.message(F.text == keyboards.BTN_BOARDING_CODE)
async def resend_boarding_code(message: Message, bot: Bot) -> None:
    ensure_user(message.from_user)
    await _send_boarding_code_for_user(message, bot)


def _passenger_recent_orders(user, *, limit: int = 5) -> list[Order]:
    u = User.get(telegram_id=user.id)
    rows = (
        Order.select()
        .where(Order.passenger_id == u.id)
        .order_by(Order.id.desc())
        .limit(limit)
    )
    return list(rows)


@router.message(F.text == keyboards.BTN_PASSENGER_CABINET)
async def passenger_cabinet(message: Message, state: FSMContext, bot: Bot) -> None:
    ensure_user(message.from_user)
    active = _active_boarding_order(message.from_user)
    orders = _passenger_recent_orders(message.from_user)
    lines = ["👤 Личный кабинет пассажира"]
    if active:
        d = Direction.get_by_id(active.direction_id)
        lines.append(
            f"\nАктивный заказ #{active.id}: {d.from_label} → {d.to_label} ({active.status})"
        )
        lines.append("Для посадки можно получить код и QR кнопкой ниже.")
    else:
        lines.append("\nАктивных заказов сейчас нет.")
    if orders:
        lines.append("\nПоследние заказы:")
        for o in orders[:3]:
            d = Direction.get_by_id(o.direction_id)
            lines.append(f"• #{o.id} · {d.from_label} → {d.to_label} · {o.status}")
    await message.answer("\n".join(lines))
    if active:
        await _send_boarding_code_for_user(message, bot)
        await message.answer(
            "Что хотите изменить?\n"
            "1) Откуда\n2) Куда\n3) Места\n4) Телефон\n5) Дата и время\n\n"
            "Отправьте номер пункта (1-5).",
            reply_markup=keyboards.cancel_back_kb(),
        )
        await state.set_state(PassengerCabinet.picking_field)
        await state.update_data(edit_order_id=active.id)


_EDIT_FIELDS = {
    "1": ("from_location", "Введите новую точку отправления:"),
    "2": ("to_location", "Введите новую точку назначения:"),
    "3": ("seats", "Введите новое количество мест (1-8):"),
    "4": ("phone", "Введите новый номер телефона:"),
    "5": ("requested_departure_at", "Введите новую дату и время (ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ.ММ):"),
}


@router.message(PassengerCabinet.picking_field, F.text)
async def passenger_pick_edit_field(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    if message.text == keyboards.BTN_BACK:
        await state.clear()
        await message.answer("Личный кабинет закрыт.", reply_markup=keyboards.main_passenger_kb())
        return
    cfg = _EDIT_FIELDS.get(message.text.strip())
    if not cfg:
        await message.answer("Выберите пункт 1-5.")
        return
    await state.update_data(edit_field=cfg[0])
    await state.set_state(PassengerCabinet.entering_value)
    await message.answer(cfg[1], reply_markup=keyboards.cancel_back_kb())


@router.message(PassengerCabinet.entering_value, F.text)
async def passenger_submit_edit_value(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    if message.text == keyboards.BTN_BACK:
        await state.set_state(PassengerCabinet.picking_field)
        await message.answer("Выберите пункт 1-5.")
        return
    data = await state.get_data()
    order_id = int(data["edit_order_id"])
    field = str(data["edit_field"])
    value_raw = message.text.strip()
    if field == "seats":
        if not value_raw.isdigit() or int(value_raw) not in range(1, 9):
            await message.answer("Введите число от 1 до 8.")
            return
        value = int(value_raw)
    elif field == "requested_departure_at":
        from app.util.time_format import parse_datetime_display

        try:
            dep = parse_datetime_display(value_raw)
        except ValueError:
            await message.answer("Неверный формат даты/времени.")
            return
        value = dep.isoformat()
    else:
        value = value_raw
    u = User.get(telegram_id=message.from_user.id)
    payload = json.dumps({field: value}, ensure_ascii=False)
    OrderChangeRequest.create(
        order_id=order_id,
        passenger_id=u.id,
        status=OrderChangeRequestStatus.PENDING.value,
        requested_payload=payload,
    )
    await state.clear()
    await message.answer(
        f"✅ Запрос на изменение заказа #{order_id} отправлен администратору.",
        reply_markup=keyboards.main_passenger_kb(),
    )


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

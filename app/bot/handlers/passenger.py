from datetime import datetime, timezone
import json

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from app.bot import keyboards
from app.bot.states import PassengerConsent, PassengerOrder, PassengerCabinet
from app.bot import messages as bot_messages
from app.bot.safe_callbacks import parse_callback_int
from app.bot.users import ensure_user
from app.config import get_settings
from app.models import (
    Direction,
    Order,
    OrderStatus,
    PassengerPaymentStatus,
    OrderChangeRequest,
    OrderChangeRequestStatus,
    PassengerProfile,
    User,
)
from app.services import order_service
from app.services import direction_search
from app.services.admin_notify import notify_new_order, notify_order_change_request
from app.services import passenger_payment_service
from app.services import scheduled_trip_service
from app.services.boarding_credentials import boarding_code_for_order

router = Router(name="passenger")

_CHANGE_FIELD_LABELS = {
    "from_location": "Откуда",
    "to_location": "Куда",
    "seats": "Мест",
    "phone": "Телефон",
    "requested_departure_at": "Желаемый выезд",
    "pickup_location": "Точка подачи",
    "pickup_time_text": "Время подачи",
}

_PASSENGER_STEP_META = {
    "requested_departure_at": {
        "label": "дату выезда",
        "prev_state": PassengerOrder.requested_departure,
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


def _normalized_display_for_field(field: str, value) -> str:
    if field == "requested_departure_at" and value:
        try:
            dep = datetime.fromisoformat(str(value))
            if dep.tzinfo is None:
                dep = dep.replace(tzinfo=timezone.utc)
            from app.util.time_format import format_departure_label

            return format_departure_label(dep)
        except Exception:
            return str(value)
    return str(value)


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
    shown = _normalized_display_for_field(field, value) if field == "requested_departure_at" else display
    await message.answer(
        f"Подтвердите {label}: {shown}",
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
    text = f"Выберите направление (стр. {page + 1}/{pages}):"
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


def _passenger_has_consent(user) -> bool:
    u = User.get(telegram_id=user.id)
    try:
        profile = PassengerProfile.get(user=u)
    except PassengerProfile.DoesNotExist:
        return False
    return bool(profile.terms_accepted_at and profile.privacy_accepted_at)


async def _start_direction_picker(message: Message, state: FSMContext) -> None:
    directions = direction_search.list_enabled_directions()
    if not directions:
        await message.answer("Направления пока недоступны.")
        return
    await state.set_state(PassengerOrder.choosing_direction)
    await state.update_data(search_results=None, dir_mode="browse")
    await _show_directions_page(message, state, 0, "browse")


async def _show_passenger_consent(message: Message, state: FSMContext) -> None:
    settings = get_settings()
    await state.set_state(PassengerConsent.consent)
    await state.update_data(terms_agreed=False, privacy_agreed=False)
    await message.answer(
        "Перед заказом поездки необходимо принять условия:\n"
        "1) откройте документы (кнопки ниже);\n"
        "2) нажмите «Согласен» у каждого пункта;\n"
        "3) нажмите «Продолжить».",
        reply_markup=keyboards.passenger_consent_kb(
            terms_agreed=False,
            privacy_agreed=False,
            terms_url=settings.passenger_terms_url,
            privacy_url=settings.passenger_privacy_url,
        ),
    )


async def continue_start_order(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user)
    if _passenger_blocked(message.from_user):
        await message.answer(
            "Доступ ограничен. Обратитесь к администратору сервиса."
        )
        return
    if not _passenger_has_consent(message.from_user):
        await _show_passenger_consent(message, state)
        return
    await _start_direction_picker(message, state)


@router.callback_query(F.data.startswith("pconsent:"))
async def passenger_consent_cb(cb: CallbackQuery, state: FSMContext) -> None:
    if _passenger_has_consent(cb.from_user):
        await cb.answer("Согласия уже приняты")
        return
    action = cb.data.split(":", 1)[1]
    data = await state.get_data()
    settings = get_settings()
    if action == "terms":
        data["terms_agreed"] = not bool(data.get("terms_agreed"))
    elif action == "privacy":
        data["privacy_agreed"] = not bool(data.get("privacy_agreed"))
    elif action == "continue":
        if not (data.get("terms_agreed") and data.get("privacy_agreed")):
            await cb.answer("Сначала примите оба согласия", show_alert=True)
            return
        from app.util.datetimeutil import utcnow

        u = User.get(telegram_id=cb.from_user.id)
        profile, _ = PassengerProfile.get_or_create(user=u)
        now = utcnow()
        profile.terms_accepted_at = now
        profile.privacy_accepted_at = now
        profile.save()
        await state.clear()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.message.answer("Согласия приняты.")
        await _start_direction_picker(cb.message, state)
        await cb.answer()
        return
    else:
        await cb.answer()
        return
    await state.set_state(PassengerConsent.consent)
    await state.update_data(**data)
    try:
        await cb.message.edit_reply_markup(
            reply_markup=keyboards.passenger_consent_kb(
                terms_agreed=bool(data.get("terms_agreed")),
                privacy_agreed=bool(data.get("privacy_agreed")),
                terms_url=settings.passenger_terms_url,
                privacy_url=settings.passenger_privacy_url,
            ),
        )
    except Exception:
        pass
    await cb.answer("Согласие принято" if action in {"terms", "privacy"} else "")


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


async def _show_route_extras(cb_or_msg, state: FSMContext, direction: Direction) -> None:
    data = await state.get_data()
    wants_pickup = bool(data.get("wants_pickup"))
    wants_dropoff = bool(data.get("wants_dropoff"))
    text = (
        f"📍 Маршрут: {direction.from_label} → {direction.to_label}\n"
        f"Место отъезда: {direction.from_label}\n"
        f"Место прибытия: {direction.to_label}\n\n"
        "Доп.услуги (цена согласуется с оператором):"
    )
    kb = keyboards.passenger_extras_kb(
        wants_pickup=wants_pickup,
        wants_dropoff=wants_dropoff,
        direction_id=direction.id,
    )
    if hasattr(cb_or_msg, "message"):
        await cb_or_msg.message.answer(text, reply_markup=kb)
    else:
        await cb_or_msg.answer(text, reply_markup=kb)


@router.callback_query(PassengerOrder.choosing_direction, F.data.startswith("dirpick:"))
async def pick_direction(cb: CallbackQuery, state: FSMContext) -> None:
    did = parse_callback_int(cb.data)
    if did is None:
        await cb.answer("Некорректные данные", show_alert=True)
        return
    try:
        direction = Direction.get_by_id(did)
    except Direction.DoesNotExist:
        await cb.answer("Направление не найдено", show_alert=True)
        return
    await state.update_data(
        direction_id=did,
        scheduled_trip_id=None,
        from_location=direction.from_label,
        to_location=direction.to_label,
        wants_pickup=False,
        wants_dropoff=False,
    )
    await state.set_state(PassengerOrder.choosing_extras)
    await _show_route_extras(cb, state, direction)
    await cb.answer()


@router.callback_query(PassengerOrder.choosing_extras, F.data.startswith("pextra:"))
async def passenger_extras_cb(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    action = parts[1]
    did = parse_callback_int(cb.data, 2)
    if did is None:
        await cb.answer("Некорректные данные", show_alert=True)
        return
    data = await state.get_data()
    if action == "pickup":
        data["wants_pickup"] = not bool(data.get("wants_pickup"))
    elif action == "dropoff":
        data["wants_dropoff"] = not bool(data.get("wants_dropoff"))
    elif action == "continue":
        await state.update_data(**data)
        await state.set_state(PassengerOrder.choosing_trip_date)
        now = datetime.now(timezone.utc)
        avail = scheduled_trip_service.available_dates_for_direction(did)
        hint = (
            "Выберите дату рейса (кнопки ниже) или «Как можно скорее» — "
            "без привязки к расписанию."
        )
        await cb.message.answer(
            hint,
            reply_markup=keyboards.trip_calendar_kb(
                now.year, now.month, available_dates=avail, direction_id=did
            ),
        )
        await cb.answer()
        return
    else:
        await cb.answer()
        return
    await state.update_data(**data)
    try:
        direction = Direction.get_by_id(did)
    except Direction.DoesNotExist:
        await cb.answer("Направление не найдено", show_alert=True)
        return
    try:
        await cb.message.edit_reply_markup(
            reply_markup=keyboards.passenger_extras_kb(
                wants_pickup=bool(data.get("wants_pickup")),
                wants_dropoff=bool(data.get("wants_dropoff")),
                direction_id=did,
            ),
        )
    except Exception:
        await _show_route_extras(cb, state, direction)
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
        await state.set_state(PassengerOrder.seats)
        await cb.message.answer("Количество мест:", reply_markup=keyboards.seats_kb())
        await cb.answer()
        return
    if action == "custom":
        from app.util.time_format import DATE_DISPLAY_HINT

        await state.update_data(scheduled_trip_id=None)
        await state.set_state(PassengerOrder.requested_departure)
        await cb.message.answer(
            "Укажите желаемую дату.\n"
            "Время отправления согласуется с оператором.\n"
            f"Формат: {DATE_DISPLAY_HINT}",
            reply_markup=keyboards.cancel_kb(),
        )
        await cb.answer()
        return
    if action in {"nav", "day", "trip"}:
        await cb.answer("Выбор дат через кнопки отключён. Используйте «Указать свою дату».", show_alert=True)
        return
    await cb.answer()


@router.message(PassengerOrder.requested_departure, F.text)
async def requested_departure_enter(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_passenger_kb())
        return
    from app.util.time_format import parse_date_display, DATE_DISPLAY_HINT
    from app.services import trip_request_service

    try:
        dep = parse_date_display(message.text.strip())
        trip_request_service.validate_requested_departure(dep)
    except ValueError as e:
        err = str(e)
        if err == "departure_in_past":
            await message.answer("Укажите дату в будущем.")
            return
        if err == "departure_too_far":
            settings = get_settings()
            await message.answer(
                f"Дата слишком далеко. Максимум на "
                f"{settings.scheduled_trip_booking_days_ahead} дней вперёд."
            )
            return
        await message.answer(f"Неверный формат. {DATE_DISPLAY_HINT}")
        return
    await state.update_data(scheduled_trip_id=None)
    from app.util.time_format import format_date_display

    accepted = format_date_display(dep)
    await message.answer(f"Принято: {accepted}")
    await _enter_passenger_step_confirmation(
        message,
        state,
        field="requested_departure_at",
        value=dep.isoformat(),
        display=accepted,
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
        from app.util.time_format import format_departure_label
        if field == "requested_departure_at" and current_val:
            try:
                dep = datetime.fromisoformat(str(current_val))
                if dep.tzinfo is None:
                    dep = dep.replace(tzinfo=timezone.utc)
                shown = format_departure_label(dep)
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
        from app.util.time_format import format_departure_label

        req_txt = ""
        if data.get("requested_departure_at"):
            dep = datetime.fromisoformat(data["requested_departure_at"])
            if dep.tzinfo is None:
                dep = dep.replace(tzinfo=timezone.utc)
            req_txt = f"\nДата: {format_departure_label(dep)}"
        extras_txt = ""
        if data.get("wants_pickup"):
            extras_txt += "\nДоп.услуга: Забрать меня"
        if data.get("wants_dropoff"):
            extras_txt += "\nДоп.услуга: Довезти до места"
        await state.set_state(PassengerOrder.confirm)
        await message.answer(
            "Проверьте заявку перед отправкой:\n"
            f"Маршрут: {direction.from_label} → {direction.to_label}{req_txt}\n"
            f"Место отъезда: {data['from_location']}\n"
            f"Место прибытия: {data['to_location']}\n"
            f"Мест: {data['seats']}\n"
            f"Телефон: {data['phone']}{extras_txt}",
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
    direction_id = data.get("direction_id")
    if not direction_id:
        await message.answer(
            "Данные заявки устарели. Начните заказ заново.",
            reply_markup=keyboards.main_passenger_kb(),
        )
        return
    try:
        direction = Direction.get_by_id(direction_id)
    except Direction.DoesNotExist:
        await message.answer(
            "Направление недоступно. Начните заказ заново.",
            reply_markup=keyboards.main_passenger_kb(),
        )
        return
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
        confirmation_code_hash="pending",
        code_issued_at=None,
        scheduled_trip_id=int(trip_id) if trip_id else None,
        scheduled_activated=scheduled_activated,
        requested_departure_at=requested_dep,
        wants_pickup=bool(data.get("wants_pickup")),
        wants_dropoff=bool(data.get("wants_dropoff")),
    )
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

        from app.util.time_format import format_departure_label

        dep_label = format_departure_label(requested_dep)
        caption = (
            f"✅ Заявка принята · #{order.id}\n"
            f"📍 {direction.from_label} → {direction.to_label}\n"
            f"📅 Желаемая дата: {dep_label}\n"
            f"Место отъезда: {order.from_location}\n"
            f"Место прибытия: {order.to_location}\n"
            f"Мест: {order.seats}\n\n"
            "Оператор подтвердит поездку и пришлёт билет с QR-кодом.\n"
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
        "Заявка принята. Оператор подтвердит поездку и пришлёт билет с QR-кодом.\n"
        "«📞 Связь с водителем» — после подтверждения. «📞 Связь с админом» — в любой момент."
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
    oid = parse_callback_int(cb.data)
    if oid is None:
        await cb.answer("Некорректные данные", show_alert=True)
        return
    try:
        order = Order.get_by_id(oid)
    except Order.DoesNotExist:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    try:
        result = passenger_payment_service.init_passenger_payment(order)
    except Exception:
        await cb.message.answer("Не удалось создать платёж. Попробуйте позже.")
        await cb.answer()
        return
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
    oid = parse_callback_int(cb.data)
    if oid is None:
        await cb.answer("Некорректные данные", show_alert=True)
        return
    try:
        order = Order.get_by_id(oid)
    except Order.DoesNotExist:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    try:
        status = passenger_payment_service.check_passenger_payment(order)
    except Exception:
        await cb.message.answer("Не удалось проверить оплату. Попробуйте позже.")
        await cb.answer()
        return
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
    if not o.passenger_ticket_sent_at:
        await message.answer(
            "Билет с QR-кодом придёт после подтверждения поездки оператором."
        )
        return
    from app.services.boarding_credentials import issue_and_send_new_credentials

    await issue_and_send_new_credentials(bot, o)
    await message.answer("Код и QR для заказа отправлены выше.")


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
        if active.passenger_ticket_sent_at:
            lines.append("Билет с QR — кнопка «🔐 Код и QR» ниже.")
        else:
            lines.append("Ожидайте билет после подтверждения оператором.")
    else:
        lines.append("\nАктивных заказов сейчас нет.")
    if orders:
        lines.append("\nПоследние заказы:")
        for o in orders[:3]:
            d = Direction.get_by_id(o.direction_id)
            lines.append(f"• #{o.id} · {d.from_label} → {d.to_label} · {o.status}")
    await message.answer("\n".join(lines))
    if active and active.passenger_ticket_sent_at:
        await _send_boarding_code_for_user(message, bot)
    if active:
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
        from app.util.time_format import format_datetime_display

        await message.answer(f"Принято: {format_datetime_display(dep)}")
        value = dep.isoformat()
    else:
        value = value_raw
    u = User.get(telegram_id=message.from_user.id)
    payload = json.dumps({field: value}, ensure_ascii=False)
    row = OrderChangeRequest.create(
        order_id=order_id,
        passenger_id=u.id,
        status=OrderChangeRequestStatus.PENDING.value,
        requested_payload=payload,
    )
    change_label = _CHANGE_FIELD_LABELS.get(field, field)
    change_value = str(value_raw)
    if field == "requested_departure_at":
        from app.util.time_format import format_datetime_display

        change_value = format_datetime_display(dep)
    passenger_label = (u.first_name or "") + (" " + u.last_name if u.last_name else "")
    passenger_label = passenger_label.strip() or (f"@{u.username}" if u.username else "Пассажир")
    try:
        await notify_order_change_request(
            message.bot,
            request_id=row.id,
            order_id=order_id,
            passenger_label=passenger_label,
            passenger_telegram_id=u.telegram_id,
            change_lines=[f"{change_label}: {change_value}"],
        )
    except Exception:
        pass
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
        if o and o.passenger_ticket_sent_at:
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
            "Связь с водителем доступна после подтверждения поездки оператором."
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

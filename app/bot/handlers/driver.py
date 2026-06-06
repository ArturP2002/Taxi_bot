from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove

from app.bot import keyboards
from app.bot import preview as preview_flow
from app.bot.states import (
    DriverRegister, ProposeDirection, DriverCode,
    DriverLoadingPhoto, DriverTransferRequest,
    DriverOnlineSetup, DriverRest, DriverOfferConsent, DriverCreateTrip,
)
from app.services import commission_service
from app.bot.users import ensure_user
from app.models import (
    CommissionLedger,
    DriverProfile,
    DriverStatus,
    Direction,
    Order,
    OrderStatus,
    OrderDriverAssignment,
    AssignmentStatus,
    ProposedDirection,
    ProposedStatus,
    QueueEntry,
    User,
    UserRole,
)
from app.config import get_settings
from app.models import PaymentRecord, PaymentStatus
from app.services import queue_service, order_service
from app.services.admin_notify import (
    notify_driver_declined,
    notify_driver_suspicious,
    notify_proposal,
    notify_trip_completed,
    notify_payment_received,
    notify_trip_started,
    notify_driver_action,
)
from app.services import driver_risk_service
from app.services import driver_registration as reg_service
from app.util.time_format import minutes_to_hours_label, parse_hours_input

router = Router(name="driver")

# Exclude menu button labels from FSM text handlers (otherwise "📊 История" becomes a city name).
_NOT_MENU_TEXT = ~F.text.in_(keyboards.DRIVER_MENU_TEXTS)

_REG_STATES = {
    "route_from": DriverRegister.route_from,
    "route_to": DriverRegister.route_to,
    "return_route": DriverRegister.return_route,
    "full_name": DriverRegister.full_name,
    "car_info": DriverRegister.car_info,
    "phone": DriverRegister.phone,
    "max_seats": DriverRegister.max_seats,
}

_DRIVER_REG_STEP_META = {
    "route_from": {
        "label": "город отправления",
        "prev_state": DriverRegister.route_from,
        "next_state": DriverRegister.route_to,
        "next_prompt": reg_service.prompt_route_to(step=2) + f"\n({keyboards.BTN_CANCEL} — выйти в меню)",
        "next_markup": "cancel",
    },
    "route_to": {
        "label": "город назначения",
        "prev_state": DriverRegister.route_to,
        "next_state": DriverRegister.return_route,
        "next_prompt": "return_route_prompt",
        "next_markup": "return",
    },
    "full_name": {
        "label": "ФИО",
        "prev_state": DriverRegister.full_name,
        "next_state": DriverRegister.car_info,
        "next_prompt": "Автомобиль (марка, модель, гос. номер):",
        "next_markup": "cancel",
    },
    "car_info": {
        "label": "данные автомобиля",
        "prev_state": DriverRegister.car_info,
        "next_state": DriverRegister.photo_front,
        "next_prompt": reg_service.REGISTRATION_PHOTOS_HINT,
        "next_markup": None,
    },
    "phone": {
        "label": "номер телефона",
        "prev_state": DriverRegister.phone,
        "next_state": DriverRegister.max_seats,
        "next_prompt": f"Всего мест в машине ({keyboards.SEATS_VEHICLE_MIN}–{keyboards.SEATS_VEHICLE_MAX}):",
        "next_markup": "cancel",
    },
    "max_seats": {
        "label": "количество мест в машине",
        "prev_state": DriverRegister.max_seats,
        "next_state": DriverRegister.price_per_seat,
        "next_prompt": "Тариф: сначала цена за одно место (₽, число), затем фикс за рейс (0 если нет):",
        "next_markup": "cancel",
    },
    "price_per_seat": {
        "label": "цену за место",
        "prev_state": DriverRegister.price_per_seat,
        "next_state": DriverRegister.fixed_price,
        "next_prompt": "Фиксированная доплата за рейс (₽, 0 если нет):",
        "next_markup": "cancel",
    },
    "fixed_price": {
        "label": "фиксированную доплату",
        "prev_state": DriverRegister.fixed_price,
        "next_state": DriverRegister.confirm,
        "next_prompt": None,
        "next_markup": None,
    },
}


def _persist_driver_reg_field(dprof: DriverProfile, field: str, val) -> None:
    dprof.status = DriverStatus.PENDING.value
    if field == "route_from":
        reg_service.save_draft_route_from(dprof, str(val))
    elif field == "route_to":
        reg_service.save_draft_route_to(dprof, str(val))
    elif field == "full_name":
        dprof.full_name = str(val)
        dprof.save()
    elif field == "car_info":
        dprof.car_info = str(val)
        dprof.save()
    elif field == "phone":
        dprof.phone = str(val)
        dprof.save()
    elif field == "max_seats":
        dprof.max_seats = int(val)
        dprof.own_seats_reserved = 0
        dprof.save()
    elif field == "price_per_seat":
        dprof.proposed_price_per_seat = Decimal(str(val))
        dprof.save()
    elif field == "fixed_price":
        dprof.proposed_fixed_price = Decimal(str(val))
        dprof.save()


async def _cancel_driver_reg_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=keyboards.main_driver_kb())


async def _cancel_or_back_to_driver_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("preview_edit_field"):
        await state.update_data(preview_edit_field=None)
        await _show_driver_preview(message, state)
        return
    await _cancel_driver_reg_flow(message, state)


async def _show_driver_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = preview_flow.format_driver_registration_preview(data)
    await state.set_state(DriverRegister.confirm)
    await message.answer(text, reply_markup=keyboards.driver_preview_kb())


async def _advance_driver_reg_after_field(
    message: Message,
    state: FSMContext,
    *,
    field: str,
    value,
    dprof: DriverProfile,
) -> None:
    updates: dict = {field: value}
    if field == "max_seats":
        updates["own_seats"] = 0
    await state.update_data(**updates)
    _persist_driver_reg_field(dprof, field, value)

    data = await state.get_data()
    edit_field = data.get("preview_edit_field")

    if field == "route_to":
        fr = data.get("route_from", "")
        await state.set_state(DriverRegister.return_route)
        await message.answer(
            f"Вы также едете обратно?\n{value} → {fr}",
            reply_markup=keyboards.return_route_kb(),
        )
        return

    if edit_field == field:
        await state.update_data(preview_edit_field=None)
        await message.answer("Готово.", reply_markup=ReplyKeyboardRemove())
        await _show_driver_preview(message, state)
        return

    meta = _DRIVER_REG_STEP_META.get(field)
    if not meta or meta["next_state"] == DriverRegister.confirm:
        await message.answer("Готово.", reply_markup=ReplyKeyboardRemove())
        await _show_driver_preview(message, state)
        return

    await state.set_state(meta["next_state"])
    next_prompt = meta["next_prompt"]
    if next_prompt == "return_route_prompt":
        fr = data.get("route_from", "")
        next_prompt = f"Вы также едете обратно?\n{value} → {fr}"
        await message.answer(next_prompt, reply_markup=keyboards.return_route_kb())
        return
    if next_prompt == reg_service.REGISTRATION_PHOTOS_HINT:
        await message.answer(next_prompt)
        return
    next_markup = keyboards.cancel_kb() if meta["next_markup"] == "cancel" else None
    await message.answer(next_prompt, reply_markup=next_markup)


def _offer_accepted(dprof: DriverProfile) -> bool:
    return bool(getattr(dprof, "offer_accepted_at", None))


async def _ensure_driver_offer_accepted(
    message: Message, state: FSMContext, dprof: DriverProfile
) -> bool:
    """Return True if user may continue; False if waiting on offer consent screen."""
    if _offer_accepted(dprof):
        return True
    settings = get_settings()
    await state.set_state(DriverOfferConsent.consent)
    await state.update_data(offer_agreed=False)
    intro = (
        "🧑‍✈️ Регистрация водителя\n\n"
        "Перед заполнением анкеты необходимо принять оферту:\n"
        "1) откройте документ (кнопка ниже), если ссылка настроена;\n"
        "2) нажмите «Согласен»;\n"
        "3) нажмите «Продолжить»."
    )
    if not settings.driver_offer_url.strip():
        intro += (
            "\n\n⚠️ Ссылка на оферту не настроена (DRIVER_OFFER_URL). "
            "Попросите администратора добавить её в настройки бота."
        )
    await message.answer(
        intro,
        reply_markup=keyboards.driver_offer_consent_kb(False, settings.driver_offer_url),
    )
    return False


@router.callback_query(F.data == "offer_toggle")
async def offer_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    u = User.get(telegram_id=cb.from_user.id)
    dprof = DriverProfile.get(user=u)
    if _offer_accepted(dprof):
        await cb.answer("Оферта уже принята")
        return
    data = await state.get_data()
    agreed = not bool(data.get("offer_agreed"))
    await state.set_state(DriverOfferConsent.consent)
    await state.update_data(offer_agreed=agreed)
    settings = get_settings()
    try:
        await cb.message.edit_reply_markup(
            reply_markup=keyboards.driver_offer_consent_kb(agreed, settings.driver_offer_url),
        )
    except Exception:
        pass
    await cb.answer("Согласие принято" if agreed else "Согласие снято")


@router.callback_query(F.data == "offer_continue")
async def offer_continue(cb: CallbackQuery, state: FSMContext) -> None:
    u = User.get(telegram_id=cb.from_user.id)
    dprof = DriverProfile.get(user=u)
    if _offer_accepted(dprof):
        await cb.answer()
        return
    data = await state.get_data()
    if not data.get("offer_agreed"):
        await cb.answer("Сначала нажмите «Согласен»", show_alert=True)
        return
    from app.util.datetimeutil import utcnow

    dprof.offer_accepted_at = utcnow()
    dprof.save()
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    from app.bot.messages import TELEGRAM_HTML

    await cb.message.answer(
        reg_service.prompt_registration_intro(),
        parse_mode=TELEGRAM_HTML,
    )
    await _resume_driver_registration(cb.message, state, dprof)
    await cb.answer()


async def begin_driver_registration(message: Message, state: FSMContext) -> None:
    """Entry: «Я водитель» or incomplete profile — start or resume анкета."""
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)

    if reg_service.driver_needs_registration(dprof) and not _offer_accepted(dprof):
        if not await _ensure_driver_offer_accepted(message, state, dprof):
            return

    await state.clear()

    if reg_service.driver_waiting_admin(dprof):
        await message.answer(
            "⏳ Анкета уже отправлена — ожидайте подтверждения администратора.\n"
            "«📞 Связь с админом» — если нужно уточнить данные.",
            reply_markup=keyboards.main_driver_kb(),
        )
        return

    if dprof.status == DriverStatus.ACTIVE.value:
        await message.answer(
            "Меню водителя:",
            reply_markup=keyboards.main_driver_kb(),
        )
        return

    if dprof.status == DriverStatus.SUSPICIOUS.value:
        await message.answer(
            "⚠️ Аккаунт на проверке администратором.\n"
            "«📞 Связь с админом» — для разъяснений.",
            reply_markup=keyboards.main_driver_kb(),
        )
        return

    if dprof.status == DriverStatus.BLOCKED.value:
        await message.answer(
            "Аккаунт заблокирован. Обратитесь к администратору.",
            reply_markup=keyboards.main_driver_kb(),
        )
        return

    if not reg_service.driver_needs_registration(dprof):
        await message.answer(
            "Меню водителя:",
            reply_markup=keyboards.main_driver_kb(),
        )
        return

    from app.bot.messages import TELEGRAM_HTML

    await message.answer(
        reg_service.prompt_registration_intro(),
        parse_mode=TELEGRAM_HTML,
    )
    await _resume_driver_registration(message, state, dprof)


async def _resume_driver_registration(
    message: Message, state: FSMContext, dprof: DriverProfile
) -> None:
    step_name, step_num = reg_service.registration_resume_step(dprof)
    route_from, route_to, include_return = reg_service.parse_draft_route(dprof)
    await state.update_data(
        route_from=route_from or "",
        route_to=route_to or "",
        include_return=include_return,
    )
    await state.set_state(_REG_STATES[step_name])
    cancel = f"\n({keyboards.BTN_CANCEL} — выйти в меню)"
    if step_name == "route_from":
        text = reg_service.prompt_route_from(step=step_num) + cancel
        await message.answer(text, reply_markup=keyboards.cancel_kb())
    elif step_name == "route_to":
        text = reg_service.prompt_route_to(step=step_num) + cancel
        await message.answer(text, reply_markup=keyboards.cancel_kb())
    elif step_name == "return_route":
        fr = route_from or "?"
        to = route_to or "?"
        await message.answer(
            f"📝 Шаг {step_num} из {reg_service.REGISTRATION_TOTAL_STEPS}\n\n"
            f"Едете обратно ({to} → {fr})?",
            reply_markup=keyboards.return_route_kb(),
        )
    elif step_name == "full_name":
        await message.answer(
            f"📝 Шаг {step_num} из {reg_service.REGISTRATION_TOTAL_STEPS}\n\nФИО:",
            reply_markup=keyboards.cancel_kb(),
        )
    elif step_name == "car_info":
        await message.answer(
            f"📝 Шаг {step_num} из {reg_service.REGISTRATION_TOTAL_STEPS}\n\n"
            "Автомобиль (марка, модель, гос. номер):",
            reply_markup=keyboards.cancel_kb(),
        )
    elif step_name == "phone":
        await message.answer(
            f"📝 Шаг {step_num} из {reg_service.REGISTRATION_TOTAL_STEPS}\n\n"
            "Номер телефона:",
            reply_markup=keyboards.cancel_kb(),
        )
    elif step_name == "max_seats":
        await message.answer(
            f"📝 Шаг {step_num} из {reg_service.REGISTRATION_TOTAL_STEPS}\n\n"
            f"Сколько мест в машине ({keyboards.SEATS_VEHICLE_MIN}–{keyboards.SEATS_VEHICLE_MAX})?",
            reply_markup=keyboards.cancel_kb(),
        )
    else:
        text = reg_service.prompt_route_from(step=1) + cancel
        await state.set_state(DriverRegister.route_from)
        await message.answer(text, reply_markup=keyboards.cancel_kb())


async def _start_registration_if_needed(
    message: Message, state: FSMContext, dprof: DriverProfile
) -> bool:
    """Return True if registration was started (caller should stop)."""
    if not reg_service.driver_needs_registration(dprof):
        return False
    if not await _ensure_driver_offer_accepted(message, state, dprof):
        return True
    await _resume_driver_registration(message, state, dprof)
    return True


def _driver(message: Message) -> DriverProfile:
    u = User.get(telegram_id=message.from_user.id)
    return DriverProfile.get(user=u)


def _driver_resting(dprof: DriverProfile) -> bool:
    until = getattr(dprof, "rest_until", None)
    if not until:
        return False
    now = datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > now


def _pending_assignment(dprof: DriverProfile):
    return (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.PENDING.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )


def _assigned_order(dprof: DriverProfile):
    return (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.ASSIGNED.value)
        )
        .order_by(Order.id.desc())
        .first()
    )


def _in_progress_order(dprof: DriverProfile):
    return (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.IN_PROGRESS.value)
        )
        .order_by(Order.id.desc())
        .first()
    )


@router.message(F.text == "🟢 Онлайн")
async def go_online(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    if u.role != UserRole.DRIVER.value:
        await message.answer("Вы не водитель.")
        return
    dprof = DriverProfile.get(user=u)
    if await _start_registration_if_needed(message, state, dprof):
        return
    if dprof.status == DriverStatus.SUSPICIOUS.value:
        await message.answer(
            "⚠️ Аккаунт на проверке администратором.\n"
            "Выход на линию временно недоступен.\n"
            "«📞 Связь с админом» — для разъяснений."
        )
        return
    if dprof.status == DriverStatus.BLOCKED.value:
        await message.answer("Аккаунт заблокирован. Обратитесь к администратору.")
        return
    if dprof.status != DriverStatus.ACTIVE.value:
        await message.answer("Ожидайте подтверждения администратора.")
        return
    if not dprof.direction_id:
        await message.answer("Вам не назначено направление.")
        return
    if _driver_resting(dprof):
        until = dprof.rest_until
        if until and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        await message.answer(
            f"Вы на отдыхе до {until.strftime('%d.%m %H:%M') if until else '—'} UTC.\n"
            "Онлайн будет доступен после отдыха."
        )
        return
    bal = Decimal(str(dprof.balance))
    lvl = order_service.debt_level(bal)
    if lvl == "block":
        await message.answer("Аккаунт заблокирован по долгу. Обратитесь к администратору.")
        return
    await state.set_state(DriverOnlineSetup.own_seats)
    await state.update_data(driver_go_online=True)
    await message.answer(
        f"Сколько мест занято вашими пассажирами ({keyboards.SEATS_OWN_MIN}–{keyboards.SEATS_OWN_MAX})?",
        reply_markup=keyboards.online_own_seats_kb(),
    )


@router.message(DriverOnlineSetup.own_seats, F.text)
async def go_online_own_seats(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) not in range(keyboards.SEATS_OWN_MIN, keyboards.SEATS_OWN_MAX + 1):
        await message.answer(f"Введите число от {keyboards.SEATS_OWN_MIN} до {keyboards.SEATS_OWN_MAX}.")
        return
    own = int(message.text)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    if own >= dprof.max_seats:
        await message.answer(f"Должно быть меньше {dprof.max_seats} (всего мест).")
        return
    await state.clear()
    DriverProfile.update(online=True, own_seats_reserved=own).where(DriverProfile.id == dprof.id).execute()
    dprof = DriverProfile.get_by_id(dprof.id)
    d = Direction.get_by_id(dprof.direction_id)
    queue_service.enqueue_driver_end(d, dprof)
    from app.services import queue_eta_service

    bal = Decimal(str(dprof.balance))
    lvl = order_service.debt_level(bal)
    msg = "Вы в очереди."
    eta = queue_eta_service.eta_for_driver(d.id, dprof.id)
    from app.services import loading_service

    snap = loading_service.driver_loading_snapshot(dprof)
    msg += f"\n{snap.status_label}"
    if eta:
        msg += f"\n⏱ Ваша очередь: {eta.label}"
    if lvl == "restrict":
        msg += " Внимание: высокий долг."
    elif lvl == "warn":
        msg += " Предупреждение: растущий долг."
    await message.answer(msg, reply_markup=keyboards.main_driver_kb())
    try:
        await notify_driver_action(message.bot, f"🟢 {dprof.full_name or dprof.id} онлайн, очередь: {d.from_label} → {d.to_label}")
    except Exception:
        pass
    try:
        from app.services.scheduler_service import check_underfill_on_direction

        await check_underfill_on_direction(message.bot, d.id)
    except Exception:
        pass


@router.message(F.text == "🔴 Оффлайн")
async def go_offline(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        u = User.get(telegram_id=message.from_user.id)
        dprof = DriverProfile.get(user=u)
    except (User.DoesNotExist, DriverProfile.DoesNotExist):
        return
    DriverProfile.update(online=False).where(DriverProfile.id == dprof.id).execute()
    if dprof.direction_id:
        queue_service.remove_from_queue(Direction.get_by_id(dprof.direction_id), dprof)
    await message.answer("Оффлайн.")
    try:
        await notify_driver_action(
            message.bot,
            f"🔴 Водитель офлайн: {dprof.full_name or dprof.id}",
        )
    except Exception:
        pass


@router.message(F.text == "📥 Мой заказ")
async def my_order(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    if u.role != UserRole.DRIVER.value:
        return
    dprof = DriverProfile.get(user=u)
    if await _start_registration_if_needed(message, state, dprof):
        return
    p = _pending_assignment(dprof)
    if p:
        o = Order.get_by_id(p.order_id)
        d = Direction.get_by_id(o.direction_id)
        comm = commission_service.commission_amount_for_order(o, dprof)
        text = (
            f"Новый заказ #{o.id}\n"
            f"{d.from_label} → {d.to_label}\n"
            f"Откуда: {o.from_location}\n"
            f"Куда: {o.to_location}\n"
            f"Мест: {o.seats}\n"
            f"Подача: {o.pickup_location or '—'} {o.pickup_time_text or ''}\n"
            f"Комиссия после QR ≈ {comm} ₽\n"
        )
        await message.answer(text, reply_markup=keyboards.assignment_inline(p.id))
        return

    o = _in_progress_order(dprof)
    if o:
        d = Direction.get_by_id(o.direction_id)
        await message.answer(
            f"🚗 Поездка #{o.id} в пути\n"
            f"{d.from_label} → {d.to_label}\n"
            f"Пассажир: {o.from_location} → {o.to_location}\n"
            f"Мест: {o.seats} | Ваши занятые: {dprof.own_seats_reserved}\n"
            f"Авто: {dprof.car_info or '—'}",
            reply_markup=keyboards.trip_actions_kb(),
        )
        return

    o = _assigned_order(dprof)
    if o:
        d = Direction.get_by_id(o.direction_id)
        summary = order_service.driver_boarding_summary(dprof)
        extra = ""
        if summary["boarded"]:
            extra = (
                f"\nПосажено: {len(summary['boarded'])} заказ(ов), "
                f"{summary['boarded_seats']} мест. Свободно: {summary['free_seats']}."
            )
        await message.answer(
            f"📋 Набор пассажиров\n"
            f"{d.from_label} → {d.to_label}{extra}\n\n"
            "«📲 Посадка (код/QR)» — отметить пассажира.\n"
            "«🚗 Выехать» — начать рейс, когда все в машине.",
            reply_markup=keyboards.before_trip_kb(),
        )
        return

    await message.answer("Нет активных назначений.")


@router.callback_query(F.data.startswith("acc:"))
async def accept(cb: CallbackQuery, state: FSMContext) -> None:
    aid = int(cb.data.split(":")[1])
    ass = OrderDriverAssignment.get_by_id(aid)
    u = User.get(telegram_id=cb.from_user.id)
    dprof = DriverProfile.get(user=u)
    if ass.driver_id != dprof.id:
        await cb.answer("Чужое назначение", show_alert=True)
        return
    try:
        order_service.driver_respond(ass, accept=True)
    except ValueError as e:
        if str(e) == "capacity_exceeded":
            from app.bot import messages as bot_messages
            from app.services.admin_notify import notify_sos_overflow
            from app.services import overflow_service

            o = Order.get_by_id(ass.order_id)
            d = Direction.get_by_id(o.direction_id)
            cap = overflow_service.direction_capacity_info(o.direction_id)
            await notify_sos_overflow(
                cb.bot,
                o.id,
                seats=o.seats,
                direction_from=d.from_label,
                direction_to=d.to_label,
                from_loc=o.from_location,
                to_loc=o.to_location,
                max_single_car_seats=cap.max_single_car_seats,
            )
            try:
                await cb.bot.send_message(
                    o.passenger.telegram_id, bot_messages.PASSENGER_OVERFLOW_MSG
                )
            except Exception:
                pass
            await cb.answer(
                "Машина уже полная. Админ переназначит пассажира на другую.",
                show_alert=True,
            )
            await cb.message.answer(
                bot_messages.DRIVER_OVERFLOW_MSG.format(order_id=o.id),
                reply_markup=keyboards.main_driver_kb(),
            )
        else:
            await cb.answer("Ошибка", show_alert=True)
        return
    o = Order.get_by_id(ass.order_id)
    dprof = DriverProfile.get_by_id(ass.driver_id)
    d = Direction.get_by_id(o.direction_id)
    from datetime import datetime, timezone

    from app.services.photo_service import new_loading_session_id

    DriverProfile.update(loading_photos_ok_at=None).where(DriverProfile.id == dprof.id).execute()
    session_id = new_loading_session_id()
    await state.update_data(
        active_order_id=o.id,
        loading_session_id=session_id,
        loading_direction_id=o.direction_id,
    )
    await state.set_state(DriverLoadingPhoto.waiting)
    pickup_hint = f"{o.pickup_location or 'уточнит админ'} {o.pickup_time_text or ''}".strip()
    from app.bot import messages as bot_messages

    await cb.message.answer(
        bot_messages.format_driver_on_loading_accept(
            route=f"{d.from_label} → {d.to_label}",
            pickup_hint=pickup_hint or "—",
        )
        + "\n\n📷 Пришлите 1–3 фото машины (кузов/салон), затем нажмите «Фото готовы».",
        reply_markup=keyboards.loading_photos_done_kb(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("dec:"))
async def decline(cb: CallbackQuery, bot: Bot) -> None:
    aid = int(cb.data.split(":")[1])
    ass = OrderDriverAssignment.get_by_id(aid)
    u = User.get(telegram_id=cb.from_user.id)
    dprof = DriverProfile.get(user=u)
    if ass.driver_id != dprof.id:
        await cb.answer("Чужое назначение", show_alert=True)
        return
    was_active = dprof.status == DriverStatus.ACTIVE.value
    order_service.driver_respond(ass, accept=False)
    await cb.message.answer("Отказ зафиксирован. Заказ возвращён администратору.")
    await cb.answer()

    order = Order.get_by_id(ass.order_id)
    dprof = DriverProfile.get_by_id(dprof.id)
    stats = driver_risk_service.driver_risk_stats(dprof.id)
    if was_active and dprof.status == DriverStatus.SUSPICIOUS.value:
        await notify_driver_suspicious(
            bot, dprof.full_name or f"ID:{dprof.id}", dprof.id, stats
        )

    new_suggestion = order_service.suggest_driver_for_order(order)

    if new_suggestion:
        new_drv = DriverProfile.get_by_id(new_suggestion.driver_id)
        from app.services.admin_notify import notify_suggestion_update
        await notify_suggestion_update(
            bot, order.id,
            suggested_driver_name=new_drv.full_name or f"ID:{new_drv.id}",
            assignment_id=new_suggestion.id,
        )
    else:
        await notify_driver_declined(
            bot,
            ass.order_id,
            dprof.full_name or "Без имени",
            driver_id=dprof.id,
            stats=stats,
        )


@router.message(F.text == "😴 Отдых")
async def rest_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    await state.set_state(DriverRest.hours)
    await message.answer(
        "Сколько часов отдыха? (число, например 5)\n"
        "После отдыха сможете снова выйти онлайн.",
        reply_markup=keyboards.cancel_kb(),
    )


@router.message(DriverRest.hours, F.text, _NOT_MENU_TEXT)
async def rest_hours(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_driver_kb())
        return
    try:
        minutes = parse_hours_input(message.text)
    except ValueError:
        await message.answer("Введите часы от 0.5 до 24.")
        return
    await state.clear()
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    DriverProfile.update(online=False, rest_until=until).where(DriverProfile.id == dprof.id).execute()
    if dprof.direction_id:
        queue_service.remove_from_queue(Direction.get_by_id(dprof.direction_id), dprof)
    hours_label = minutes_to_hours_label(minutes)
    await message.answer(
        f"Отдых {hours_label} до {until.strftime('%d.%m %H:%M')} UTC.\nВы сняты с линии.",
        reply_markup=keyboards.main_driver_kb(),
    )
    await notify_driver_action(
        bot,
        f"😴 {dprof.full_name or dprof.id}: отдых {hours_label} (до {until.strftime('%H:%M')} UTC)",
    )


@router.message(F.text.in_({"📲 Посадка (код/QR)", "▶️ Старт поездки"}))
async def board_passenger_prompt(message: Message, state: FSMContext) -> None:
    cur = await state.get_state()
    if cur == DriverCode.waiting_code.state:
        await message.answer("Уже жду код. Введите 6 цифр, QR или фото QR.")
        return
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    if _in_progress_order(dprof):
        await message.answer("Рейс уже в пути.", reply_markup=keyboards.trip_actions_kb())
        return
    summary = order_service.driver_boarding_summary(dprof)
    if not summary["orders"]:
        await message.answer("Нет принятых заказов. Откройте «📥 Мой заказ».")
        return
    await state.set_state(DriverCode.waiting_code)
    await state.update_data(boarding_mode=True)
    await message.answer(
        "Посадка пассажира (код или QR):\n\n"
        "• 6 цифр кода\n"
        "• 📷 фото QR с экрана\n"
        "• скан QR камерой (откроется бот)\n\n"
        "Можно отметить нескольких пассажиров. "
        "Выезд — отдельно кнопкой «🚗 Выехать».",
        reply_markup=keyboards.cancel_kb(),
    )


@router.message(F.text == "🚗 Выехать")
async def depart_trip(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    if _in_progress_order(dprof):
        await message.answer("Рейс уже в пути.", reply_markup=keyboards.trip_actions_kb())
        return
    ok, key, info = order_service.depart_driver_trip(dprof)
    if not ok:
        from app.services import code_service

        await message.answer(code_service.verification_error_label(key))
        if key == "no_boarded_passengers":
            summary = order_service.driver_boarding_summary(dprof)
            if summary["waiting_boarding"]:
                await message.answer(
                    "Сначала «📲 Посадка (код/QR)» для каждого пассажира."
                )
        return
    d = Direction.get_by_id(dprof.direction_id)
    from app.bot import messages as bot_messages

    await message.answer(
        bot_messages.format_driver_departure_status(summary=info, direction=d),
        reply_markup=keyboards.trip_actions_kb(),
    )
    for o in info.get("departed_orders") or []:
        try:
            await bot.send_message(
                o.passenger.telegram_id,
                f"🚗 Поездка #{o.id} началась. Приятной дороги!",
            )
        except Exception:
            pass
        await notify_trip_started(
            bot,
            o.id,
            dprof.full_name or f"ID:{dprof.id}",
            route=f"{d.from_label} → {d.to_label}",
            seats=o.seats,
            car_info=dprof.car_info,
            own_seats=int(dprof.own_seats_reserved or 0),
        )


@router.message(DriverCode.waiting_code, F.text, _NOT_MENU_TEXT)
async def enter_code(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Ввод кода отменён.", reply_markup=keyboards.before_trip_kb())
        return
    from app.services import code_service

    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    data = await state.get_data()
    raw = message.text.strip()
    parsed = code_service.parse_verification_raw(
        raw,
        default_order_id=data.get("active_order_id"),
    )
    if parsed:
        try:
            o = Order.get_by_id(parsed.order_id)
        except Order.DoesNotExist:
            await message.answer("Заказ не найден.")
            return
    else:
        o = order_service.find_order_for_driver_boarding_code(dprof.id, raw)
        if not o:
            await message.answer(
                "Код не найден среди ваших пассажиров на посадку.\n"
                "Проверьте 6 цифр или отсканируйте QR."
            )
            return
    ok, key = order_service.verify_passenger_boarding(
        o,
        raw,
        driver_id=dprof.id,
        expected_order_id=o.id,
    )
    if not ok:
        await message.answer(code_service.verification_error_label(key))
        return
    await _after_passenger_boarded(message, state, bot, dprof=dprof, order=Order.get_by_id(o.id))


async def _after_passenger_boarded(
    message: Message,
    state: FSMContext,
    bot: Bot,
    *,
    dprof: DriverProfile,
    order: Order,
) -> None:
    from app.bot import messages as bot_messages

    dprof = DriverProfile.get_by_id(dprof.id)
    summary = order_service.driver_boarding_summary(dprof)
    await state.clear()
    await message.answer(
        bot_messages.format_driver_boarding_status(order=order, summary=summary),
        reply_markup=keyboards.before_trip_kb(),
    )
    try:
        await bot.send_message(
            order.passenger.telegram_id,
            f"✅ Водитель отметил вашу посадку (заказ #{order.id}). "
            "Ожидайте выезда.",
        )
    except Exception:
        pass


@router.message(DriverCode.waiting_code, F.photo | F.document)
async def enter_code_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    from app.services import code_service

    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.answer("Пришлите фото с QR-кодом.")
        return

    try:
        tg_file = await bot.get_file(file_id)
        buf = await bot.download_file(tg_file.file_path)
        image_bytes = buf.read()
    except Exception:
        await message.answer("Не удалось загрузить фото. Попробуйте снова или введите 6 цифр.")
        return

    payloads = code_service.decode_qr_from_image_bytes(image_bytes)
    if not payloads:
        await message.answer(
            "QR на фото не распознан.\n"
            "Сделайте чёткий снимок экрана пассажира или введите 6 цифр кода."
        )
        return

    last_err = "invalid_format"
    for raw in payloads:
        parsed = code_service.parse_verification_raw(raw)
        if parsed:
            try:
                o = Order.get_by_id(parsed.order_id)
            except Order.DoesNotExist:
                continue
        else:
            o = order_service.find_order_for_driver_boarding_code(dprof.id, raw)
            if not o:
                continue
        ok, key = order_service.verify_passenger_boarding(
            o, raw, driver_id=dprof.id, expected_order_id=o.id
        )
        if ok:
            await _after_passenger_boarded(
                message, state, bot, dprof=dprof, order=Order.get_by_id(o.id)
            )
            return
        last_err = key
    await message.answer(code_service.verification_error_label(last_err))


@router.message(F.text == "🔁 Встать обратно")
async def return_queue_flag(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    active = (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.IN_PROGRESS.value)
        )
        .first()
    )
    if not active:
        await message.answer("Нет активной поездки.")
        return
    direction = Direction.get_by_id(active.direction_id)
    rev_id = direction.reverse_direction_id
    if not rev_id:
        await message.answer("Обратное направление не настроено администратором.")
        return
    DriverProfile.update(pending_return_direction_id=rev_id).where(DriverProfile.id == dprof.id).execute()
    await message.answer("После завершения поездки вы будете поставлены в конец очереди обратного направления.")


@router.message(F.text == "💬 Связь с пассажиром")
async def driver_chat_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    active = (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status.in_([OrderStatus.ASSIGNED.value, OrderStatus.IN_PROGRESS.value]))
        )
        .order_by(Order.id.desc())
        .first()
    )
    if not active:
        await message.answer("Нет заказа для связи (нужно принять назначение).")
        return
    o = Order.get_by_id(active.id)
    tid = o.passenger.telegram_id
    await message.answer(
        f"Заказ #{active.id} — напишите пассажиру:",
        reply_markup=keyboards.contact_user_inline(tid, "💬 Пассажир"),
    )


@router.message(F.text == keyboards.BTN_CANCEL)
async def cancel_active_flow(message: Message, state: FSMContext) -> None:
    if not await state.get_state():
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=keyboards.main_driver_kb())


@router.message(F.text == "✅ Завершить поездку")
async def complete_trip(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    active = (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.IN_PROGRESS.value)
        )
        .first()
    )
    if not active:
        await message.answer("Нет активной поездки.")
        return
    ok, key = order_service.complete_order(active, dprof)
    if not ok:
        await message.answer(f"Нельзя завершить: {key}")
        return
    await state.clear()
    await message.answer("Поездка завершена.", reply_markup=keyboards.main_driver_kb())
    try:
        await bot.send_message(active.passenger.telegram_id, "Спасибо за поездку!")
    except Exception:
        pass
    commission = CommissionLedger.select().where(CommissionLedger.order_id == active.id).first()
    comm_amount = commission.amount if commission else "0"
    await notify_trip_completed(bot, active.id, dprof.full_name or "Без имени", comm_amount)


@router.message(F.text == "💸 Оплатить долг")
async def pay_debt(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    bal = Decimal(str(dprof.balance))
    if bal <= 0:
        await message.answer("У вас нет долга.")
        return

    from app.models import PaymentPayerType
    from app.services.payment_provider import get_payment_provider
    settings = get_settings()
    if not settings.shop_id or not settings.shop_secret_key:
        pr = PaymentRecord.create(
            driver=dprof,
            payer_type=PaymentPayerType.DRIVER.value,
            amount=bal,
            status=PaymentStatus.AWAITING_ADMIN.value,
            provider="manual",
        )
        await message.answer(
            f"Сумма к оплате: {bal} руб.\nПереведите администратору и дождитесь подтверждения в админке."
        )
        await notify_payment_received(bot, dprof.full_name or "Без имени", bal, pr.id)
        return
    provider = get_payment_provider()
    try:
        result = provider.create_payment(
            amount=bal,
            description=f"Комиссия водителя #{dprof.id}",
            return_url="https://t.me",
            metadata={"driver_id": dprof.id},
        )
    except Exception:
        await message.answer("Ошибка при создании платежа. Попробуйте позже.")
        return

    pr = PaymentRecord.create(
        driver=dprof,
        payer_type=PaymentPayerType.DRIVER.value,
        amount=bal,
        status=PaymentStatus.PENDING.value,
        provider="yookassa",
        provider_ref=result["payment_id"],
        raw_payload=str(result.get("raw", "")),
    )

    url = result.get("confirmation_url", "")
    if url:
        await message.answer(
            f"Сумма к оплате: {bal} руб.\n\n"
            f"Перейдите по ссылке для оплаты:\n{url}\n\n"
            "После оплаты нажмите «🔍 Проверить платёж»."
        )
    else:
        await message.answer(
            f"Платёж создан (ID: {result['payment_id']}). Сумма: {bal} руб.\n"
            "После оплаты нажмите «🔍 Проверить платёж»."
        )
    await notify_payment_received(bot, dprof.full_name or "Без имени", bal, pr.id)


@router.message(F.text == "🔍 Проверить платёж")
async def check_payment(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)

    pending_payments = list(
        PaymentRecord.select()
        .where(
            (PaymentRecord.driver_id == dprof.id)
            & (PaymentRecord.status == PaymentStatus.PENDING.value)
            & (PaymentRecord.provider == "yookassa")
        )
        .order_by(PaymentRecord.created_at.desc())
        .limit(5)
    )
    if not pending_payments:
        await message.answer("Нет ожидающих платежей. Сначала нажмите «💸 Оплатить долг».")
        return

    from app.services.payment_provider import get_payment_provider
    provider = get_payment_provider()
    confirmed_total = Decimal("0")

    for pr in pending_payments:
        payment_id = pr.provider_ref
        if not payment_id:
            continue
        try:
            info = provider.check_payment(payment_id)
        except Exception:
            continue

        if info["status"] == "succeeded" and info["paid"]:
            amount = Decimal(str(pr.amount))
            confirmed_total += amount
            PaymentRecord.update(
                status=PaymentStatus.CONFIRMED.value
            ).where(PaymentRecord.id == pr.id).execute()
        elif info["status"] == "canceled":
            PaymentRecord.update(
                status=PaymentStatus.FAILED.value
            ).where(PaymentRecord.id == pr.id).execute()

    if confirmed_total > 0:
        new_bal = Decimal(str(dprof.balance)) - confirmed_total
        if new_bal < 0:
            new_bal = Decimal("0")
        DriverProfile.update(balance=new_bal).where(DriverProfile.id == dprof.id).execute()
        from app.services import audit_service
        audit_service.log_action(
            "payment_auto_confirmed",
            actor_telegram_id=u.telegram_id,
            entity_type="driver",
            entity_id=str(dprof.id),
            payload={"confirmed_amount": str(confirmed_total), "new_balance": str(new_bal)},
        )
        await message.answer(
            f"Оплата подтверждена: {confirmed_total} руб.\n"
            f"Новый баланс (долг): {new_bal} руб."
        )
    else:
        await message.answer(
            "Платёж ещё не прошёл. Подождите пару минут и нажмите «🔍 Проверить платёж» снова."
        )


@router.message(F.text == "💰 Баланс")
async def balance(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    bal = Decimal(str(dprof.balance))
    lvl = order_service.debt_level(bal)
    s = get_settings()
    await message.answer(
        f"Баланс (долг): {bal} ₽. Уровень: {lvl}.\n"
        f"Комиссия {s.commission_percent}% от тарифа по направлению начисляется после QR.\n"
        "Оплата: «💸 Оплатить долг» (YooKassa) или перевод с подтверждением админом."
    )


@router.message(F.text == "ℹ️ Как считается долг")
async def debt_info(message: Message, state: FSMContext) -> None:
    await state.clear()
    s = get_settings()
    await message.answer(
        f"Комиссия = {s.commission_percent}% × (цена_за_место × места платформы + фикс).\n"
        "Свои пассажиры (указаны при «Онлайн») не входят в комиссию.\n"
        "Начисление — сразу после сканирования QR/кода пассажира."
    )


@router.message(F.text == "👥 Мои пассажиры")
async def my_passengers(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    rows = list(
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.ASSIGNED.value)
        )
    )
    if not rows:
        await message.answer("Нет принятых пассажиров на загрузке.")
        return
    occ = order_service.occupied_seats_for_driver(dprof)
    from app.services import loading_service

    snap = loading_service.driver_loading_snapshot(dprof)
    lines = [f"#{o.id}: {o.from_location} → {o.to_location}, {o.seats} мест" for o in rows]
    extra = "\nПодача, время и доплата за подачу задаёт админ."
    await message.answer(
        f"{snap.status_label}\n"
        f"Пассажиры ({len(rows)} заказов, {occ} мест):\n" + "\n".join(lines)
        + f"\n\nСвободно для платформы: {snap.free_seats} мест"
        + extra
    )


@router.message(F.text == "🧭 Направление")
async def driver_direction_info(message: Message) -> None:
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    dir_name = "—"
    if dprof.direction_id:
        d = Direction.get_by_id(dprof.direction_id)
        dir_name = f"{d.from_label} → {d.to_label}"
    await message.answer(
        f"Текущее направление: {dir_name}\n\n"
        "Сменить направление может только администратор.\n"
        "Напишите в «📞 Связь с админом» — укажите желаемый маршрут."
    )


@router.message(F.text == "📞 Связь с админом")
async def driver_admin_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings()
    if not settings.admin_ids:
        await message.answer("Администратор не настроен.")
        return
    await message.answer(
        "Связь с администратором:",
        reply_markup=keyboards.contact_admins_inline(settings.admin_ids),
    )


@router.message(F.text == "📅 Мои рейсы")
async def driver_my_trips(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    dprof = _driver(message)
    if await _start_registration_if_needed(message, state, dprof):
        return
    from app.services import scheduled_trip_service

    trips = scheduled_trip_service.list_driver_trips(dprof)
    if not trips:
        await message.answer("Нет запланированных рейсов.")
        return
    lines = []
    for t in trips:
        from app.util.time_format import format_datetime_display

        label = format_datetime_display(t.departure_at)
        free = scheduled_trip_service.seats_available(t)
        d = Direction.get_by_id(t.direction_id)
        lines.append(f"• {d.from_label}→{d.to_label} · {label} · свободно {free}/{t.seats_total}")
    await message.answer("📅 Ваши рейсы:\n" + "\n".join(lines))


@router.message(F.text == "➕ Объявить рейс")
async def driver_create_trip_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    dprof = _driver(message)
    if await _start_registration_if_needed(message, state, dprof):
        return
    settings = get_settings()
    if not settings.driver_can_create_trips:
        await message.answer("Создание рейсов доступно только через администратора.")
        return
    if not dprof.direction_id:
        await message.answer("Сначала администратор должен назначить вам направление.")
        return
    await state.set_state(DriverCreateTrip.date)
    await message.answer(
        "Дата и время выезда (ДД.ММ.ГГГГ ЧЧ:ММ), например 25.05.2026 08:00:",
        reply_markup=keyboards.cancel_kb(),
    )


@router.message(DriverCreateTrip.date, F.text, _NOT_MENU_TEXT)
async def driver_create_trip_datetime(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_driver_kb())
        return
    from app.util.time_format import DATETIME_DISPLAY_HINT, parse_datetime_display

    try:
        dep = parse_datetime_display(message.text.strip())
    except ValueError:
        await message.answer(f"Формат: {DATETIME_DISPLAY_HINT}")
        return
    from app.util.time_format import format_datetime_display

    await state.update_data(trip_departure_at=dep.isoformat())
    await message.answer(f"Принято: {format_datetime_display(dep)}")
    await state.set_state(DriverCreateTrip.seats)
    dprof = _driver(message)
    await message.answer(f"Сколько мест в рейсе (1–{dprof.max_seats or 8})?")


@router.message(DriverCreateTrip.seats, F.text, _NOT_MENU_TEXT)
async def driver_create_trip_seats(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Отменено.", reply_markup=keyboards.main_driver_kb())
        return
    if not message.text.isdigit():
        await message.answer("Введите число.")
        return
    seats = int(message.text)
    dprof = _driver(message)
    max_s = dprof.max_seats or keyboards.SEATS_VEHICLE_MAX
    if seats < 1 or seats > max_s:
        await message.answer(f"От 1 до {max_s}.")
        return
    data = await state.get_data()
    dep = datetime.fromisoformat(data["trip_departure_at"])
    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=timezone.utc)
    from app.services import scheduled_trip_service
    from app.models.scheduled_trip import ScheduledTripCreatedBy, ScheduledTripStatus

    settings = get_settings()
    status = (
        ScheduledTripStatus.OPEN.value
        if settings.driver_can_create_trips
        else ScheduledTripStatus.DRAFT.value
    )
    trip = scheduled_trip_service.create_trip(
        direction_id=dprof.direction_id,
        departure_at=dep,
        seats_total=seats,
        driver_id=dprof.id,
        created_by=ScheduledTripCreatedBy.DRIVER.value,
        status=status,
    )
    from app.util.time_format import format_datetime_display

    await state.clear()
    label = "открыт" if status == ScheduledTripStatus.OPEN.value else "на модерации"
    await message.answer(
        f"✅ Рейс создан ({label}): {format_datetime_display(dep)} · {seats} мест.",
        reply_markup=keyboards.main_driver_kb(),
    )


@router.message(F.text == "📊 История")
async def history(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    rows = (
        CommissionLedger.select()
        .where(CommissionLedger.driver_id == dprof.id)
        .order_by(CommissionLedger.created_at.desc())
        .limit(10)
    )
    lines = [f"order {c.order_id}: +{c.amount} (base {c.base_fare})" for c in rows]
    await message.answer("\n".join(lines) if lines else "Пока пусто.")


@router.message(F.text == "➕ Предложить маршрут")
async def propose_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    await state.set_state(ProposeDirection.from_label)
    await message.answer("Откуда (город):")


@router.message(ProposeDirection.from_label, F.text, _NOT_MENU_TEXT)
async def propose_from(message: Message, state: FSMContext) -> None:
    await state.update_data(from_label=message.text.strip())
    await state.set_state(ProposeDirection.to_label)
    await message.answer("Куда (город):")


@router.message(ProposeDirection.to_label, F.text, _NOT_MENU_TEXT)
async def propose_to(message: Message, state: FSMContext) -> None:
    to_city = message.text.strip()
    data = await state.get_data()
    await state.update_data(to_label=to_city)
    await state.set_state(ProposeDirection.return_route)
    fr = data.get("from_label", "")
    await message.answer(
        f"Едете обратно ({to_city} → {fr})?",
        reply_markup=keyboards.return_route_kb(),
    )


@router.callback_query(ProposeDirection.return_route, F.data.in_({"return_yes", "return_no"}))
async def propose_return(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(include_return=cb.data == "return_yes")
    await state.set_state(ProposeDirection.eta_min)
    await cb.message.answer("Примерное время в пути (часы, числом, например 3 или 3.5):")
    await cb.answer()


@router.message(ProposeDirection.eta_min, F.text, _NOT_MENU_TEXT)
async def propose_eta(message: Message, state: FSMContext) -> None:
    try:
        eta_min = parse_hours_input(message.text)
    except ValueError:
        await message.answer("Введите часы от 0.5 до 72 (например 3 или 4.5).")
        return
    await state.update_data(eta_min=eta_min)
    await state.set_state(ProposeDirection.comment)
    await message.answer("Комментарий (или «-»):")


@router.message(ProposeDirection.comment, F.text, _NOT_MENU_TEXT)
async def propose_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    from app.services import reserve_service

    comment = None if message.text.strip() == "-" else message.text.strip()
    include_return = data.get("include_return", False)
    _, pos, activated = reserve_service.create_reserved_paired_proposals(
        dprof,
        data["from_label"],
        data["to_label"],
        estimated_time_min=data["eta_min"],
        comment=comment,
        include_return=include_return,
    )
    lead = (
        ProposedDirection.select()
        .where(
            (ProposedDirection.proposer_id == dprof.id)
            & (ProposedDirection.from_label == data["from_label"])
        )
        .order_by(ProposedDirection.created_at.desc())
        .first()
    )
    if lead:
        grp = getattr(lead, "reserve_group_id", None)
        total = len(reserve_service.unique_proposers_in_group(grp)) if grp else 1
        await reserve_service.notify_reserve_status(
            bot, lead, position=pos, total=total, activated=activated,
        )
    msg = f"Заявка отправлена: {data['from_label']} → {data['to_label']}"
    if include_return:
        msg += f"\n↩ и {data['to_label']} → {data['from_label']}"
    await message.answer(msg)
    await notify_proposal(
        bot, data["from_label"], data["to_label"], dprof.full_name or "Без имени",
        paired=include_return,
    )


@router.message(DriverRegister.route_from, F.text, _NOT_MENU_TEXT)
async def reg_route_from(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    ok, result = reg_service.validate_single_city(message.text)
    if not ok:
        await message.answer(result, reply_markup=keyboards.cancel_kb())
        return
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="route_from", value=result, dprof=dprof,
    )


@router.message(DriverRegister.route_to, F.text, _NOT_MENU_TEXT)
async def reg_route_to(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    ok, result = reg_service.validate_single_city(message.text)
    if not ok:
        await message.answer(result, reply_markup=keyboards.cancel_kb())
        return
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="route_to", value=result, dprof=dprof,
    )


@router.callback_query(DriverRegister.return_route, F.data.in_({"return_yes", "return_no"}))
async def reg_return_route(cb: CallbackQuery, state: FSMContext) -> None:
    include_return = cb.data == "return_yes"
    await state.update_data(include_return=include_return)
    data = await state.get_data()  # refreshed after include_return
    route_to = (data.get("route_to") or "").strip()
    if route_to:
        ensure_user(cb.from_user, prefer_driver=True)
        dprof = DriverProfile.get(user=User.get(telegram_id=cb.from_user.id))
        reg_service.save_draft_return_choice(dprof, route_to, include_return)
    if data.get("preview_edit_field"):
        await state.update_data(preview_edit_field=None)
        await cb.message.answer("Данные обновлены.")
        await _show_driver_preview(cb.message, state)
        await cb.answer()
        return
    await state.set_state(DriverRegister.full_name)
    await cb.message.answer("ФИО:")
    await cb.answer()


@router.message(DriverRegister.return_route, F.text, _NOT_MENU_TEXT)
async def reg_return_route_text(message: Message) -> None:
    await message.answer(
        "Сначала выберите «Да, еду обратно» или «Нет, только туда» кнопкой под сообщением выше."
    )


@router.message(DriverRegister.full_name, F.text, _NOT_MENU_TEXT)
async def reg_name(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Введите ФИО (минимум 2 символа).")
        return
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="full_name", value=name, dprof=dprof,
    )


@router.message(DriverRegister.car_info, F.text, _NOT_MENU_TEXT)
async def reg_car(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    car_info = message.text.strip()
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="car_info", value=car_info, dprof=dprof,
    )


def _reg_photo_file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id
    return None


async def _after_driver_photos(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("preview_edit_field") == "photos":
        await state.update_data(preview_edit_field=None)
        await _show_driver_preview(message, state)
        return
    await state.set_state(DriverRegister.phone)
    await message.answer("Номер телефона:", reply_markup=keyboards.cancel_kb())


async def _reg_photo_step(
    message: Message,
    state: FSMContext,
    *,
    kind: str,
    next_state,
    next_prompt: str,
    sort_order: int = 0,
    reply_markup=None,
) -> bool:
    fid = _reg_photo_file_id(message)
    if not fid:
        await message.answer("Пришлите фото (изображение).")
        return False
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    from app.services.photo_service import save_registration_photo

    save_registration_photo(dprof.id, kind, fid, sort_order=sort_order)
    await state.set_state(next_state)
    await message.answer(next_prompt, reply_markup=reply_markup)
    return True


@router.message(DriverRegister.photo_front, F.photo | F.document)
async def reg_photo_front(message: Message, state: FSMContext) -> None:
    await _reg_photo_step(
        message, state, kind="front", next_state=DriverRegister.photo_back,
        next_prompt="📷 Фото сзади:",
    )


@router.message(DriverRegister.photo_back, F.photo | F.document)
async def reg_photo_back(message: Message, state: FSMContext) -> None:
    await _reg_photo_step(
        message, state, kind="back", next_state=DriverRegister.photo_left,
        next_prompt="📷 Фото слева (бок):",
    )


@router.message(DriverRegister.photo_left, F.photo | F.document)
async def reg_photo_left(message: Message, state: FSMContext) -> None:
    await _reg_photo_step(
        message, state, kind="left", next_state=DriverRegister.photo_right,
        next_prompt="📷 Фото справа (бок):",
    )


@router.message(DriverRegister.photo_right, F.photo | F.document)
async def reg_photo_right(message: Message, state: FSMContext) -> None:
    await _reg_photo_step(
        message, state, kind="right", next_state=DriverRegister.photo_salon,
        next_prompt="📷 Фото салона (1):",
    )


@router.message(DriverRegister.photo_salon, F.photo | F.document)
async def reg_photo_salon(message: Message, state: FSMContext) -> None:
    await _reg_photo_step(
        message, state, kind="salon", next_state=DriverRegister.photo_salon_extra,
        next_prompt="📷 Второе фото салона:",
        sort_order=0,
        reply_markup=keyboards.skip_salon_extra_kb(),
    )


@router.message(DriverRegister.photo_salon_extra, F.text == keyboards.BTN_CANCEL)
async def reg_photo_salon_cancel(message: Message, state: FSMContext) -> None:
    await _cancel_or_back_to_driver_preview(message, state)


@router.message(DriverRegister.photo_salon_extra, F.text == "⏭️ Без второго фото салона")
async def reg_photo_salon_skip(message: Message, state: FSMContext) -> None:
    await _after_driver_photos(message, state)


@router.message(DriverRegister.photo_salon_extra, F.text, _NOT_MENU_TEXT)
async def reg_photo_salon_extra_hint(message: Message) -> None:
    await message.answer(
        "Пришлите второе фото салона или нажмите «⏭️ Без второго фото салона».",
        reply_markup=keyboards.skip_salon_extra_kb(),
    )


@router.message(DriverRegister.photo_salon_extra, F.photo | F.document)
async def reg_photo_salon_extra(message: Message, state: FSMContext) -> None:
    fid = _reg_photo_file_id(message)
    if not fid:
        await message.answer("Пришлите фото (изображение).")
        return
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    from app.services.photo_service import save_registration_photo

    save_registration_photo(dprof.id, "salon2", fid, sort_order=1)
    await _after_driver_photos(message, state)


@router.message(DriverRegister.phone, F.text, _NOT_MENU_TEXT)
async def reg_phone(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    phone = message.text.strip()
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="phone", value=phone, dprof=dprof,
    )


@router.message(DriverRegister.max_seats, F.text, _NOT_MENU_TEXT)
async def reg_max_seats(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    if not message.text.isdigit() or int(message.text) not in range(
        keyboards.SEATS_VEHICLE_MIN, keyboards.SEATS_VEHICLE_MAX + 1
    ):
        await message.answer(
            f"Введите число {keyboards.SEATS_VEHICLE_MIN}–{keyboards.SEATS_VEHICLE_MAX}."
        )
        return
    max_seats = int(message.text)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="max_seats", value=max_seats, dprof=dprof,
    )


@router.message(DriverRegister.price_per_seat, F.text, _NOT_MENU_TEXT)
async def reg_price(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    try:
        price = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Введите число.")
        return
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="price_per_seat", value=str(price), dprof=dprof,
    )


@router.message(DriverRegister.fixed_price, F.text, _NOT_MENU_TEXT)
async def reg_fixed(message: Message, state: FSMContext) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await _cancel_or_back_to_driver_preview(message, state)
        return
    try:
        fixed = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Введите число.")
        return
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await _advance_driver_reg_after_field(
        message, state, field="fixed_price", value=str(fixed), dprof=dprof,
    )


@router.callback_query(DriverRegister.confirm, F.data.startswith("dprev:"))
async def driver_preview_cb(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    action = cb.data.split(":", 1)[1]
    if action == "cancel":
        await state.clear()
        await cb.message.answer("Отменено.", reply_markup=keyboards.main_driver_kb())
        await cb.answer()
        return
    if action == "edit":
        await cb.message.edit_reply_markup(reply_markup=keyboards.driver_preview_edit_kb())
        await cb.answer()
        return
    if action == "back":
        await cb.message.edit_reply_markup(reply_markup=keyboards.driver_preview_kb())
        await cb.answer()
        return
    if action.startswith("field:"):
        field = action.split(":", 1)[1]
        data = await state.get_data()
        await state.update_data(preview_edit_field=field)
        if field == "route_from":
            await state.set_state(DriverRegister.route_from)
            await cb.message.answer(
                reg_service.prompt_route_from(step=1)
                + f"\n({keyboards.BTN_CANCEL} — вернуться к предпросмотру)",
                reply_markup=keyboards.cancel_kb(),
            )
        elif field == "route_to":
            await state.set_state(DriverRegister.route_to)
            await cb.message.answer(
                reg_service.prompt_route_to(step=2)
                + f"\n({keyboards.BTN_CANCEL} — вернуться к предпросмотру)",
                reply_markup=keyboards.cancel_kb(),
            )
        elif field == "return_route":
            route_to = data.get("route_to", "")
            route_from = data.get("route_from", "")
            await state.set_state(DriverRegister.return_route)
            await cb.message.answer(
                f"Вы также едете обратно?\n{route_to} → {route_from}",
                reply_markup=keyboards.return_route_kb(),
            )
        elif field == "full_name":
            await state.set_state(DriverRegister.full_name)
            await cb.message.answer("ФИО:", reply_markup=keyboards.cancel_kb())
        elif field == "car_info":
            await state.set_state(DriverRegister.car_info)
            await cb.message.answer(
                "Автомобиль (марка, модель, гос. номер):",
                reply_markup=keyboards.cancel_kb(),
            )
        elif field == "photos":
            await state.set_state(DriverRegister.photo_front)
            await cb.message.answer(reg_service.REGISTRATION_PHOTOS_HINT)
        elif field == "phone":
            await state.set_state(DriverRegister.phone)
            await cb.message.answer("Номер телефона:", reply_markup=keyboards.cancel_kb())
        elif field == "max_seats":
            await state.set_state(DriverRegister.max_seats)
            await cb.message.answer(
                f"Всего мест в машине ({keyboards.SEATS_VEHICLE_MIN}–{keyboards.SEATS_VEHICLE_MAX}):",
                reply_markup=keyboards.cancel_kb(),
            )
        elif field == "price_per_seat":
            await state.set_state(DriverRegister.price_per_seat)
            await cb.message.answer(
                "Цена за одно место (₽, число):",
                reply_markup=keyboards.cancel_kb(),
            )
        elif field == "fixed_price":
            await state.set_state(DriverRegister.fixed_price)
            await cb.message.answer(
                "Фиксированная доплата за рейс (₽, 0 если нет):",
                reply_markup=keyboards.cancel_kb(),
            )
        else:
            await cb.answer("Неизвестное поле", show_alert=True)
            return
        await cb.answer()
        return
    if action != "submit":
        await cb.answer()
        return

    data = await state.get_data()
    message = cb.message
    ensure_user(cb.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=cb.from_user.id))
    ok, result = await reg_service.finalize_driver_registration(
        bot,
        dprof=dprof,
        telegram_id=cb.from_user.id,
        data=data,
    )
    if not ok:
        if result == "route_lost":
            await state.set_state(DriverRegister.route_from)
            await message.answer(
                reg_service.prompt_route_from(step=1)
                + f"\n({keyboards.BTN_CANCEL} — выйти в меню)",
                reply_markup=keyboards.cancel_kb(),
            )
            await cb.answer()
            return
        if result == "ФИО не указано. Введите ФИО:":
            await state.set_state(DriverRegister.full_name)
        await message.answer(result)
        await cb.answer()
        return

    await state.clear()
    await message.answer(result, reply_markup=keyboards.main_driver_kb())
    await cb.answer()


@router.message(DriverLoadingPhoto.waiting, F.photo | F.document)
async def loading_photo_upload(message: Message, state: FSMContext) -> None:
    fid = _reg_photo_file_id(message)
    if not fid:
        await message.answer("Пришлите фото.")
        return
    data = await state.get_data()
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    from app.services.photo_service import save_loading_photo

    save_loading_photo(
        dprof.id,
        int(data.get("loading_direction_id") or dprof.direction_id or 0),
        data.get("loading_session_id", "default"),
        fid,
    )
    await message.answer("Фото сохранено. Можно отправить ещё или нажать «Фото готовы».")


@router.callback_query(F.data == "loading_photos_done")
async def loading_photos_done(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    u = User.get(telegram_id=cb.from_user.id)
    dprof = DriverProfile.get(user=u)
    from app.services.photo_service import confirm_loading_photos
    from app.services.loading_notify import broadcast_loading_update

    confirm_loading_photos(dprof)
    data = await state.get_data()
    direction_id = data.get("loading_direction_id") or dprof.direction_id
    if direction_id:
        session_id = data.get("loading_session_id")
        await broadcast_loading_update(
            bot,
            int(direction_id),
            loading_session_id=session_id,
        )
    await state.set_state(None)
    await cb.message.answer(
        "✅ Загрузка опубликована.\n"
        "Пассажирам отправлены фото машины и статус набора.\n"
        "Дальше: «📲 Посадка (код/QR)» для каждого, затем «🚗 Выехать».",
        reply_markup=keyboards.before_trip_kb(),
    )
    await cb.answer()


@router.message(F.text == "🔄 Передать пассажира админу")
async def transfer_to_admin_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    rows = list(
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status.in_([OrderStatus.ASSIGNED.value, OrderStatus.IN_PROGRESS.value]))
        )
    )
    if not rows:
        await message.answer("Нет активных пассажиров для передачи.")
        return
    if len(rows) == 1:
        await state.update_data(transfer_order_id=rows[0].id)
    else:
        lines = "\n".join(f"#{o.id}: {o.from_location}" for o in rows)
        await message.answer(f"Укажите номер заказа в ответе:\n{lines}")
    await state.set_state(DriverTransferRequest.note)
    await message.answer("Комментарий для админа (или «-»):")


@router.message(DriverTransferRequest.note, F.text, _NOT_MENU_TEXT)
async def transfer_to_admin_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    from datetime import datetime, timezone

    from app.services.admin_notify import notify_driver_transfer_request

    data = await state.get_data()
    note = None if message.text.strip() == "-" else message.text.strip()
    oid = data.get("transfer_order_id")
    if not oid and message.text.strip().isdigit():
        oid = int(message.text.strip())
    if not oid:
        await message.answer("Укажите номер заказа.")
        return
    o = Order.get_by_id(oid)
    now = datetime.now(timezone.utc)
    Order.update(transfer_requested_at=now, transfer_note=note).where(Order.id == o.id).execute()
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    await notify_driver_transfer_request(
        bot, order_id=o.id, driver_name=dprof.full_name or str(dprof.id), note=note,
    )
    await state.clear()
    await message.answer(
        "Запрос отправлен администратору. Ожидайте пересадку в другую машину.",
        reply_markup=keyboards.before_trip_kb(),
    )

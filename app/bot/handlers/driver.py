from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from app.bot import keyboards
from app.bot.states import (
    DriverRegister, ProposeDirection, DriverCode, DriverRelayChat,
    DriverLoadingPhoto, DriverTransferRequest,
    DriverOnlineSetup, DriverRest, AdminRelayChat,
)
from app.services import commission_service
from app.services import admin_relay
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
    if not dprof.full_name:
        await state.set_state(DriverRegister.route_from)
        await message.answer(
            "Сначала заполните анкету. Маршрут — откуда (город):\n"
            f"({keyboards.BTN_CANCEL} — выйти в меню)",
            reply_markup=keyboards.cancel_kb(),
        )
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
    from app.services.direction_pairs import get_reverse_direction
    from app.services import queue_eta_service

    rev = get_reverse_direction(d)
    bal = Decimal(str(dprof.balance))
    lvl = order_service.debt_level(bal)
    msg = "Вы в очереди."
    eta = queue_eta_service.eta_for_driver(d.id, dprof.id)
    from app.services import loading_service

    snap = loading_service.driver_loading_snapshot(dprof)
    msg += f"\n{snap.status_label}"
    if eta:
        msg += f"\n⏱ Ваша очередь: {eta.label}"
        if not eta.is_now:
            msg += f" (~{eta.loading_at.strftime('%d.%m %H:%M')} UTC)"
    if rev:
        msg += f"\n↩ Обратный рейс: {rev.from_label} → {rev.to_label} (после поездки — «🔁 Встать обратно»)."
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
    if not dprof.full_name:
        await state.set_state(DriverRegister.route_from)
        await message.answer(
            "Сначала заполните анкету. Маршрут — откуда (город):\n"
            f"({keyboards.BTN_CANCEL} — выйти в меню)",
            reply_markup=keyboards.cancel_kb(),
        )
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
        await message.answer(
            f"📋 Заказ #{o.id} принят, ожидает старта\n"
            f"{d.from_label} → {d.to_label}\n"
            f"Мест: {o.seats}\n\n"
            "Нажмите «▶️ Старт поездки» и введите 6 цифр кода или QR от пассажира.",
            reply_markup=keyboards.before_trip_kb(),
        )
        await state.update_data(active_order_id=o.id)
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

            o = Order.get_by_id(ass.order_id)
            d = Direction.get_by_id(o.direction_id)
            await notify_sos_overflow(
                cb.bot,
                o.id,
                seats=o.seats,
                direction_from=d.from_label,
                direction_to=d.to_label,
                from_loc=o.from_location,
                to_loc=o.to_location,
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


@router.message(F.text == "▶️ Старт поездки")
async def start_trip_prompt(message: Message, state: FSMContext) -> None:
    cur = await state.get_state()
    if cur == DriverCode.waiting_code.state:
        await message.answer("Уже жду код. Введите 6 цифр или QR от пассажира.")
        return
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    o = _assigned_order(dprof)
    if not o:
        if _in_progress_order(dprof):
            await message.answer("Поездка уже начата.", reply_markup=keyboards.trip_actions_kb())
        else:
            await message.answer("Нет заказа для старта. Откройте «📥 Мой заказ».")
        return
    await state.set_state(DriverCode.waiting_code)
    await state.update_data(active_order_id=o.id)
    await message.answer(
        f"Старт заказа #{o.id}.\n"
        "Введите 6 цифр кода от пассажира или вставьте QR-токен одним сообщением.",
        reply_markup=keyboards.cancel_kb(),
    )


@router.message(DriverCode.waiting_code, F.text, _NOT_MENU_TEXT)
async def enter_code(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text == keyboards.BTN_CANCEL:
        await state.clear()
        await message.answer("Ввод кода отменён.", reply_markup=keyboards.before_trip_kb())
        return
    data = await state.get_data()
    oid = data.get("active_order_id")
    if not oid:
        await state.clear()
        return
    o = Order.get_by_id(oid)
    ok, key = order_service.verify_order_code(o, message.text.strip())
    if not ok:
        await message.answer(f"Ошибка: {key}")
        return
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    dprof = DriverProfile.get_by_id(dprof.id)
    comm = CommissionLedger.select().where(CommissionLedger.order_id == o.id).first()
    comm_txt = f" Начислена комиссия: {comm.amount} ₽." if comm else ""
    d = Direction.get_by_id(o.direction_id)
    await state.clear()
    await message.answer(
        f"✅ Поездка #{o.id} началась.{comm_txt}\n"
        f"Маршрут: {d.from_label} → {d.to_label}\n"
        f"Мест: {o.seats} | Свои: {dprof.own_seats_reserved}\n"
        f"Долг: {dprof.balance} ₽",
        reply_markup=keyboards.trip_actions_kb(),
    )
    try:
        await bot.send_message(o.passenger.telegram_id, "Водитель подтвердил код. Приятной поездки!")
    except Exception:
        pass
    await notify_trip_started(
        bot, o.id, dprof.full_name or f"ID:{dprof.id}",
        route=f"{d.from_label} → {d.to_label}",
        seats=o.seats,
        car_info=dprof.car_info,
        own_seats=int(dprof.own_seats_reserved or 0),
    )


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
        await message.answer("Нет заказа для чата (нужно принять назначение).")
        return
    await state.set_state(DriverRelayChat.active)
    await state.update_data(relay_order_id=active.id)
    await message.answer(f"Чат по заказу #{active.id}. Пишите текст. /stop чтобы выйти.")


@router.message(DriverRelayChat.active, F.text)
async def driver_relay(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text.startswith("/stop"):
        await state.clear()
        await message.answer("Чат закрыт.", reply_markup=keyboards.main_driver_kb())
        return
    data = await state.get_data()
    oid = data.get("relay_order_id")
    o = Order.get_by_id(oid)
    text = f"💬 Заказ #{oid} (водитель):\n{message.text}"
    try:
        await bot.send_message(o.passenger.telegram_id, text)
    except Exception:
        await message.answer("Не удалось доставить.")
        return
    await message.answer("Отправлено.")


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
    ensure_user(message.from_user, prefer_driver=True)
    await state.set_state(AdminRelayChat.active)
    await state.update_data(admin_relay_driver_id=_driver(message).id)
    await message.answer("Чат с администратором. /stop — выход.")


@router.message(AdminRelayChat.active, F.text)
async def driver_admin_relay(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text.startswith("/stop"):
        await state.clear()
        await message.answer("Чат закрыт.", reply_markup=keyboards.main_driver_kb())
        return
    data = await state.get_data()
    await admin_relay.relay_to_admins(
        bot,
        message.text,
        from_telegram_id=message.from_user.id,
        role="водитель",
        driver_id=data.get("admin_relay_driver_id"),
    )
    await message.answer("Отправлено администратору.")


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
        grp = lead.reserve_group_id
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
    route_from = message.text.strip()
    await state.update_data(route_from=route_from)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    reg_service.save_draft_route_from(dprof, route_from)
    await state.set_state(DriverRegister.route_to)
    await message.answer("Куда (город):")


@router.message(DriverRegister.route_to, F.text, _NOT_MENU_TEXT)
async def reg_route_to(message: Message, state: FSMContext) -> None:
    to_city = message.text.strip()
    data = await state.get_data()
    await state.update_data(route_to=to_city)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    reg_service.save_draft_route_to(dprof, to_city)
    await state.set_state(DriverRegister.return_route)
    fr = data.get("route_from", "")
    await message.answer(
        f"Вы также едете обратно?\n{to_city} → {fr}",
        reply_markup=keyboards.return_route_kb(),
    )


@router.callback_query(DriverRegister.return_route, F.data.in_({"return_yes", "return_no"}))
async def reg_return_route(cb: CallbackQuery, state: FSMContext) -> None:
    include_return = cb.data == "return_yes"
    await state.update_data(include_return=include_return)
    data = await state.get_data()
    route_to = (data.get("route_to") or "").strip()
    if route_to:
        ensure_user(cb.from_user, prefer_driver=True)
        dprof = DriverProfile.get(user=User.get(telegram_id=cb.from_user.id))
        reg_service.save_draft_return_choice(dprof, route_to, include_return)
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
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Введите ФИО (минимум 2 символа).")
        return
    await state.update_data(full_name=name)
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    dprof.full_name = name
    dprof.status = DriverStatus.PENDING.value
    dprof.save()
    await state.set_state(DriverRegister.car_info)
    await message.answer("Автомобиль (марка, модель, гос. номер):")


@router.message(DriverRegister.car_info, F.text, _NOT_MENU_TEXT)
async def reg_car(message: Message, state: FSMContext) -> None:
    car_info = message.text.strip()
    await state.update_data(car_info=car_info)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    dprof.car_info = car_info
    dprof.status = DriverStatus.PENDING.value
    dprof.save()
    await state.set_state(DriverRegister.photo_front)
    await message.answer("📷 Фото машины спереди:")


def _reg_photo_file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id
    return None


async def _reg_photo_step(
    message: Message,
    state: FSMContext,
    *,
    kind: str,
    next_state,
    next_prompt: str,
    sort_order: int = 0,
) -> bool:
    fid = _reg_photo_file_id(message)
    if not fid:
        await message.answer("Пришлите фото (изображение).")
        return False
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    from app.services.photo_service import save_registration_photo

    save_registration_photo(dprof.id, kind, fid, sort_order=sort_order)
    await state.set_state(next_state)
    await message.answer(next_prompt)
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
        next_prompt="📷 Второе фото салона (или «⏭ Без второго фото салона»):",
        sort_order=0,
    )


@router.message(DriverRegister.photo_salon_extra, F.text == "⏭ Без второго фото салона")
async def reg_photo_salon_skip(message: Message, state: FSMContext) -> None:
    await state.set_state(DriverRegister.phone)
    await message.answer("Номер телефона:", reply_markup=keyboards.cancel_kb())


@router.message(DriverRegister.photo_salon_extra, F.photo | F.document)
async def reg_photo_salon_extra(message: Message, state: FSMContext) -> None:
    if await _reg_photo_step(
        message, state, kind="salon2", next_state=DriverRegister.phone,
        next_prompt="Номер телефона:", sort_order=1,
    ):
        await message.answer("Номер телефона:", reply_markup=keyboards.cancel_kb())


@router.message(DriverRegister.phone, F.text, _NOT_MENU_TEXT)
async def reg_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    await state.update_data(phone=phone)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    dprof.phone = phone
    dprof.status = DriverStatus.PENDING.value
    dprof.save()
    await state.set_state(DriverRegister.max_seats)
    await message.answer(
        f"Всего мест в машине ({keyboards.SEATS_VEHICLE_MIN}–{keyboards.SEATS_VEHICLE_MAX}):"
    )


@router.message(DriverRegister.max_seats, F.text, _NOT_MENU_TEXT)
async def reg_max_seats(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) not in range(
        keyboards.SEATS_VEHICLE_MIN, keyboards.SEATS_VEHICLE_MAX + 1
    ):
        await message.answer(
            f"Введите число {keyboards.SEATS_VEHICLE_MIN}–{keyboards.SEATS_VEHICLE_MAX}."
        )
        return
    max_seats = int(message.text)
    await state.update_data(max_seats=max_seats)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    dprof.max_seats = max_seats
    dprof.status = DriverStatus.PENDING.value
    dprof.save()
    await state.set_state(DriverRegister.own_seats)
    await message.answer(
        f"Сколько мест обычно занимают ваши пассажиры "
        f"({keyboards.SEATS_OWN_MIN}–{keyboards.SEATS_OWN_MAX})?"
    )


@router.message(DriverRegister.own_seats, F.text, _NOT_MENU_TEXT)
async def reg_own_seats(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    max_seats = int(data.get("max_seats", keyboards.SEATS_VEHICLE_MAX))
    if not message.text.isdigit() or int(message.text) not in range(
        keyboards.SEATS_OWN_MIN, keyboards.SEATS_OWN_MAX + 1
    ):
        await message.answer(
            f"Введите число {keyboards.SEATS_OWN_MIN}–{keyboards.SEATS_OWN_MAX}."
        )
        return
    own = int(message.text)
    if own >= max_seats:
        await message.answer(f"Должно быть меньше {max_seats} (всего мест в машине).")
        return
    await state.update_data(own_seats=own)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    dprof.own_seats_reserved = own
    dprof.status = DriverStatus.PENDING.value
    dprof.save()
    await state.set_state(DriverRegister.price_per_seat)
    await message.answer("Тариф: цена за место (₽, число):")


@router.message(DriverRegister.price_per_seat, F.text, _NOT_MENU_TEXT)
async def reg_price(message: Message, state: FSMContext) -> None:
    try:
        price = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Введите число.")
        return
    await state.update_data(price_per_seat=str(price))
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    dprof.proposed_price_per_seat = price
    dprof.status = DriverStatus.PENDING.value
    dprof.save()
    await state.set_state(DriverRegister.fixed_price)
    await message.answer("Фиксированная доплата за рейс (₽, 0 если нет):")


@router.message(DriverRegister.fixed_price, F.text, _NOT_MENU_TEXT)
async def reg_fixed(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        fixed = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Введите число.")
        return
    data = await state.get_data()
    data["fixed_price"] = str(fixed)
    ensure_user(message.from_user, prefer_driver=True)
    dprof = DriverProfile.get(user=User.get(telegram_id=message.from_user.id))
    dprof.proposed_fixed_price = fixed
    dprof.status = DriverStatus.PENDING.value
    dprof.save()

    ok, result = await reg_service.finalize_driver_registration(
        bot,
        dprof=dprof,
        telegram_id=message.from_user.id,
        data=data,
    )
    if not ok:
        if result == "route_lost":
            await state.set_state(DriverRegister.route_from)
            await message.answer(
                "Данные маршрута не сохранились. Укажите снова — откуда (город):",
                reply_markup=keyboards.cancel_kb(),
            )
            return
        if result.startswith("own_seats:"):
            max_seats = int(result.split(":")[1])
            await state.set_state(DriverRegister.own_seats)
            await message.answer(
                f"Своих мест должно быть меньше {max_seats}. "
                f"Введите число {keyboards.SEATS_OWN_MIN}–{max_seats - 1}:"
            )
            return
        if result == "ФИО не указано. Введите ФИО:":
            await state.set_state(DriverRegister.full_name)
        await message.answer(result)
        return

    await state.clear()
    await message.answer(result, reply_markup=keyboards.main_driver_kb())


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
        await broadcast_loading_update(bot, int(direction_id))
    await state.set_state(None)
    await cb.message.answer(
        "✅ Загрузка опубликована. Пассажиры и очередь уведомлены.\n"
        "«▶️ Старт поездки» — после посадки и кода/QR.",
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

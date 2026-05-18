from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from app.bot import keyboards
from app.bot.states import (
    DriverRegister, ProposeDirection, DriverCode, DriverRelayChat,
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
    User,
    UserRole,
)
from app.config import get_settings
from app.models import PaymentRecord, PaymentStatus
from app.services import queue_service, order_service
from app.services.admin_notify import (
    notify_driver_registered, notify_proposal, notify_driver_declined,
    notify_trip_completed, notify_payment_received, notify_trip_started,
    notify_driver_action,
)
from app.util.time_format import minutes_to_hours_label, parse_hours_input

router = Router(name="driver")


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
    if await state.get_state():
        return
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    if u.role != UserRole.DRIVER.value:
        await message.answer("Вы не водитель.")
        return
    dprof = DriverProfile.get(user=u)
    if not dprof.full_name:
        await state.set_state(DriverRegister.route_from)
        await message.answer("Сначала заполните анкету. Маршрут — откуда (город):")
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
        "Сколько мест занято вашими пассажирами (0–6)?",
        reply_markup=keyboards.online_own_seats_kb(),
    )


@router.message(DriverOnlineSetup.own_seats, F.text)
async def go_online_own_seats(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) not in range(0, 7):
        await message.answer("Введите число от 0 до 6.")
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
    if eta:
        msg += f"\n⏱ Загрузка: {eta.label}"
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


@router.message(F.text == "🔴 Оффлайн")
async def go_offline(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        return
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
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    if u.role != UserRole.DRIVER.value:
        return
    dprof = DriverProfile.get(user=u)
    if not dprof.full_name:
        await state.set_state(DriverRegister.route_from)
        await message.answer("Сначала заполните анкету. Маршрут — откуда (город):")
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
    order_service.driver_respond(ass, accept=True)
    o = Order.get_by_id(ass.order_id)
    dprof = DriverProfile.get_by_id(ass.driver_id)
    from app.services.admin_notify import notify_driver_loading
    qe = (
        QueueEntry.select()
        .where(QueueEntry.direction_id == o.direction_id)
        .order_by(QueueEntry.position)
        .first()
    )
    if qe and qe.driver_id == dprof.id:
        nxt = queue_service.next_in_queue_after(o.direction_id, qe.position)
        if nxt:
            d = Direction.get_by_id(o.direction_id)
            from app.services import queue_eta_service

            nxt_slot = queue_eta_service.eta_for_driver(d.id, nxt.id)
            await notify_driver_loading(
                cb.bot, nxt.user.telegram_id,
                dprof.full_name or "Водитель",
                f"{d.from_label} → {d.to_label}",
                qe.position + 1,
                loading_label=nxt_slot.label if nxt_slot else None,
            )
    await state.update_data(active_order_id=o.id)
    await cb.message.answer(
        f"Заказ #{o.id} принят.\n"
        "Когда посадите пассажира — «▶️ Старт поездки» и код/QR от пассажира.",
        reply_markup=keyboards.before_trip_kb(),
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
    order_service.driver_respond(ass, accept=False)
    await cb.message.answer("Отказ зафиксирован. Заказ возвращён администратору.")
    await cb.answer()

    order = Order.get_by_id(ass.order_id)
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
        await notify_driver_declined(bot, ass.order_id, dprof.full_name or "Без имени")


@router.message(F.text == "😴 Отдых")
async def rest_start(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        return
    ensure_user(message.from_user, prefer_driver=True)
    await state.set_state(DriverRest.hours)
    await message.answer(
        "Сколько часов отдыха? (число, например 5)\n"
        "После отдыха сможете снова выйти онлайн.",
        reply_markup=keyboards.cancel_kb(),
    )


@router.message(DriverRest.hours, F.text)
async def rest_hours(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text == "❌ Отмена":
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
    if cur and cur != DriverCode.waiting_code.state:
        return
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


@router.message(DriverCode.waiting_code, F.text)
async def enter_code(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text in {
        "🔁 Встать обратно", "✅ Завершить поездку", "💬 Связь с пассажиром",
        "▶️ Старт поездки",
    }:
        return
    if message.text == "❌ Отмена":
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


@router.message(F.text == "✅ Завершить поездку")
async def complete_trip(message: Message, state: FSMContext, bot: Bot) -> None:
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
    if await state.get_state():
        return
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
    if await state.get_state():
        return
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
    if await state.get_state():
        return
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
    if await state.get_state():
        return
    s = get_settings()
    await message.answer(
        f"Комиссия = {s.commission_percent}% × (цена_за_место × места платформы + фикс).\n"
        "Свои пассажиры (указаны при «Онлайн») не входят в комиссию.\n"
        "Начисление — сразу после сканирования QR/кода пассажира."
    )


@router.message(F.text == "👥 Мои пассажиры")
async def my_passengers(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        return
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
    lines = [f"#{o.id}: {o.from_location} → {o.to_location}, мест {o.seats}" for o in rows]
    await message.answer(
        f"Пассажиры ({len(rows)} заказов, {occ} мест):\n" + "\n".join(lines)
        + f"\n\nСвободно мест платформы: {order_service.platform_capacity_remaining(dprof)}"
    )


@router.message(F.text == "📞 Связь с админом")
async def driver_admin_chat(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        return
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
    if await state.get_state():
        return
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
    ensure_user(message.from_user, prefer_driver=True)
    await state.set_state(ProposeDirection.from_label)
    await message.answer("Откуда (город):")


@router.message(ProposeDirection.from_label, F.text)
async def propose_from(message: Message, state: FSMContext) -> None:
    await state.update_data(from_label=message.text.strip())
    await state.set_state(ProposeDirection.to_label)
    await message.answer("Куда (город):")


@router.message(ProposeDirection.to_label, F.text)
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


@router.message(ProposeDirection.eta_min, F.text)
async def propose_eta(message: Message, state: FSMContext) -> None:
    try:
        eta_min = parse_hours_input(message.text)
    except ValueError:
        await message.answer("Введите часы от 0.5 до 72 (например 3 или 4.5).")
        return
    await state.update_data(eta_min=eta_min)
    await state.set_state(ProposeDirection.comment)
    await message.answer("Комментарий (или «-»):")


@router.message(ProposeDirection.comment, F.text)
async def propose_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    from app.services import direction_pairs

    comment = None if message.text.strip() == "-" else message.text.strip()
    include_return = data.get("include_return", False)
    direction_pairs.create_paired_proposals(
        dprof,
        data["from_label"],
        data["to_label"],
        estimated_time_min=data["eta_min"],
        comment=comment,
        include_return=include_return,
    )
    msg = f"Заявка отправлена: {data['from_label']} → {data['to_label']}"
    if include_return:
        msg += f"\n↩ и {data['to_label']} → {data['from_label']}"
    await message.answer(msg)
    await notify_proposal(
        bot, data["from_label"], data["to_label"], dprof.full_name or "Без имени",
        paired=include_return,
    )


@router.message(DriverRegister.route_from, F.text)
async def reg_route_from(message: Message, state: FSMContext) -> None:
    await state.update_data(route_from=message.text.strip())
    await state.set_state(DriverRegister.route_to)
    await message.answer("Куда (город):")


@router.message(DriverRegister.route_to, F.text)
async def reg_route_to(message: Message, state: FSMContext) -> None:
    to_city = message.text.strip()
    data = await state.get_data()
    await state.update_data(route_to=to_city)
    await state.set_state(DriverRegister.return_route)
    fr = data.get("route_from", "")
    await message.answer(
        f"Вы также едете обратно?\n{to_city} → {fr}",
        reply_markup=keyboards.return_route_kb(),
    )


@router.callback_query(DriverRegister.return_route, F.data.in_({"return_yes", "return_no"}))
async def reg_return_route(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(include_return=cb.data == "return_yes")
    await state.set_state(DriverRegister.full_name)
    await cb.message.answer("ФИО:")
    await cb.answer()


@router.message(DriverRegister.full_name, F.text)
async def reg_name(message: Message, state: FSMContext) -> None:
    await state.update_data(full_name=message.text.strip())
    await state.set_state(DriverRegister.car_info)
    await message.answer("Автомобиль (марка, модель, гос. номер):")


@router.message(DriverRegister.car_info, F.text)
async def reg_car(message: Message, state: FSMContext) -> None:
    await state.update_data(car_info=message.text.strip())
    await state.set_state(DriverRegister.phone)
    await message.answer("Номер телефона:")


@router.message(DriverRegister.phone, F.text)
async def reg_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.text.strip())
    await state.set_state(DriverRegister.max_seats)
    await message.answer("Всего мест в машине (1–6):")


@router.message(DriverRegister.max_seats, F.text)
async def reg_max_seats(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) not in range(1, 7):
        await message.answer("Введите число 1–6.")
        return
    await state.update_data(max_seats=int(message.text))
    await state.set_state(DriverRegister.own_seats)
    await message.answer("Сколько мест обычно занимают ваши пассажиры (0–6)?")


@router.message(DriverRegister.own_seats, F.text)
async def reg_own_seats(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) not in range(0, 7):
        await message.answer("Введите число 0–6.")
        return
    await state.update_data(own_seats=int(message.text))
    await state.set_state(DriverRegister.price_per_seat)
    await message.answer("Тариф: цена за место (₽, число):")


@router.message(DriverRegister.price_per_seat, F.text)
async def reg_price(message: Message, state: FSMContext) -> None:
    try:
        price = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Введите число.")
        return
    await state.update_data(price_per_seat=str(price))
    await state.set_state(DriverRegister.fixed_price)
    await message.answer("Фиксированная доплата за рейс (₽, 0 если нет):")


@router.message(DriverRegister.fixed_price, F.text)
async def reg_fixed(message: Message, state: FSMContext, bot: Bot) -> None:
    import logging
    logger = logging.getLogger("taxi_bot.driver")
    try:
        fixed = Decimal(message.text.replace(",", ".").strip())
    except Exception:
        await message.answer("Введите число.")
        return
    data = await state.get_data()
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    max_seats = data.get("max_seats", 6)
    own_seats = data.get("own_seats", 0)
    price = Decimal(data.get("price_per_seat", "0"))
    DriverProfile.update(
        full_name=data.get("full_name", ""),
        car_info=data.get("car_info", ""),
        phone=data.get("phone", ""),
        max_seats=max_seats,
        own_seats_reserved=own_seats,
        proposed_price_per_seat=price,
        proposed_fixed_price=fixed,
        status=DriverStatus.PENDING.value,
    ).where(DriverProfile.id == dprof.id).execute()
    from app.services import direction_pairs

    include_return = data.get("include_return", True)
    proposals = direction_pairs.create_paired_proposals(
        dprof,
        data["route_from"],
        data["route_to"],
        max_seats=max_seats,
        own_seats=own_seats,
        price_per_seat=price,
        fixed_price=fixed,
        comment=f"Анкета: {data.get('car_info', '')}",
        include_return=include_return,
    )
    route_txt = f"{data['route_from']} → {data['route_to']}"
    if include_return and len(proposals) > 1:
        route_txt += f"\n↩ {data['route_to']} → {data['route_from']}"
    summary = (
        "✅ Анкета отправлена!\n\n"
        f"Маршрут(ы): {route_txt}\n"
        f"ФИО: {data.get('full_name')}\n"
        f"Авто: {data.get('car_info')}\n"
        f"Тел: {data.get('phone')}\n"
        f"Мест: {max_seats} (своих: {own_seats})\n"
        f"Тариф: {price} ₽/место + {fixed} ₽ фикс\n\n"
        "Ожидайте подтверждения. «📞 Связь с админом» — в любой момент."
    )
    await message.answer(summary, reply_markup=keyboards.main_driver_kb())
    try:
        await notify_driver_registered(
            bot,
            data.get("full_name", ""),
            message.from_user.id,
            route=f"{data['route_from']} → {data['route_to']}",
            max_seats=max_seats,
            tariff=f"{price}/{fixed}",
        )
        await notify_proposal(
            bot, data["route_from"], data["route_to"], data.get("full_name", ""),
            paired=include_return,
        )
    except Exception as e:
        logger.warning("Notify failed: %s", e)

from decimal import Decimal

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from app.bot import keyboards
from app.bot.states import DriverRegister, ProposeDirection, DriverCode, DriverRelayChat
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
    notify_trip_completed, notify_payment_received,
)

router = Router(name="driver")


def _driver(message: Message) -> DriverProfile:
    u = User.get(telegram_id=message.from_user.id)
    return DriverProfile.get(user=u)


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
        await state.set_state(DriverRegister.full_name)
        await message.answer("Сначала заполните анкету. ФИО:")
        return
    if dprof.status != DriverStatus.ACTIVE.value:
        await message.answer("Ожидайте подтверждения администратора.")
        return
    if not dprof.direction_id:
        await message.answer("Вам не назначено направление.")
        return
    bal = Decimal(str(dprof.balance))
    lvl = order_service.debt_level(bal)
    if lvl == "block":
        await message.answer("Аккаунт заблокирован по долгу. Обратитесь к администратору.")
        return
    DriverProfile.update(online=True).where(DriverProfile.id == dprof.id).execute()
    d = Direction.get_by_id(dprof.direction_id)
    queue_service.enqueue_driver_end(d, dprof)
    msg = "Вы в очереди."
    if lvl == "restrict":
        msg += " Внимание: высокий долг."
    elif lvl == "warn":
        msg += " Предупреждение: растущий долг."
    await message.answer(msg)


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


@router.message(F.text == "📥 Мой заказ")
async def my_order(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    if u.role != UserRole.DRIVER.value:
        return
    dprof = DriverProfile.get(user=u)
    if not dprof.full_name:
        await state.set_state(DriverRegister.full_name)
        await message.answer("Сначала заполните анкету. ФИО:")
        return
    pending = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.PENDING.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
    )
    p = pending.first()
    if p:
        o = Order.get_by_id(p.order_id)
        d = Direction.get_by_id(o.direction_id)
        text = (
            f"Новый заказ #{o.id}\n"
            f"{d.from_label} → {d.to_label}\n"
            f"Откуда: {o.from_location}\n"
            f"Куда: {o.to_location}\n"
            f"Мест: {o.seats}\n"
            f"Подача: {o.pickup_location or '—'} {o.pickup_time_text or ''}\n"
        )
        await message.answer(text, reply_markup=keyboards.assignment_inline(p.id))
        return

    active = (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == dprof.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.IN_PROGRESS.value)
        )
    )
    o = active.first()
    if o:
        await message.answer(
            f"Поездка #{o.id} в пути. Введите 6 цифр кода или вставьте QR-токен одним сообщением.\n"
            f"Мест: {o.seats}",
            reply_markup=keyboards.trip_actions_kb(),
        )
        await state.set_state(DriverCode.waiting_code)
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
    await state.set_state(DriverCode.waiting_code)
    await state.update_data(active_order_id=o.id)
    await cb.message.answer(
        f"Заказ #{o.id} принят. Запросите код у пассажира и введите 6 цифр или вставьте QR-токен сообщением."
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


@router.message(DriverCode.waiting_code, F.text)
async def enter_code(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text in {"🔁 Встать обратно", "✅ Завершить поездку", "💬 Связь с пассажиром"}:
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
    await state.clear()
    await message.answer("Код принят. Поездка началась.", reply_markup=keyboards.trip_actions_kb())
    try:
        await bot.send_message(o.passenger.telegram_id, "Водитель подтвердил код. Приятной поездки!")
    except Exception:
        pass


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
            & (Order.status == OrderStatus.IN_PROGRESS.value)
        )
        .first()
    )
    if not active:
        await message.answer("Нет активной поездки для чата.")
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

    from app.services.payment_provider import get_payment_provider
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
    await message.answer(
        f"Баланс (долг): {bal}. Уровень: {lvl}.\n"
        f"Оплату подтверждает администратор после перевода (кнопка «Я оплатил» в разработке через кассу)."
    )


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
    await state.update_data(to_label=message.text.strip())
    await state.set_state(ProposeDirection.eta_min)
    await message.answer("Примерное время в пути (минуты, числом):")


@router.message(ProposeDirection.eta_min, F.text)
async def propose_eta(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit():
        await message.answer("Введите число минут.")
        return
    await state.update_data(eta_min=int(message.text))
    await state.set_state(ProposeDirection.comment)
    await message.answer("Комментарий (или «-»):")


@router.message(ProposeDirection.comment, F.text)
async def propose_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    u = User.get(telegram_id=message.from_user.id)
    dprof = DriverProfile.get(user=u)
    comment = None if message.text.strip() == "-" else message.text.strip()
    ProposedDirection.create(
        proposer=dprof,
        from_label=data["from_label"],
        to_label=data["to_label"],
        estimated_time_min=data["eta_min"],
        comment=comment,
        status=ProposedStatus.PENDING.value,
    )
    await message.answer("Заявка отправлена администратору.")
    await notify_proposal(bot, data["from_label"], data["to_label"], dprof.full_name or "Без имени")


@router.message(DriverRegister.full_name, F.text)
async def reg_name(message: Message, state: FSMContext) -> None:
    await state.update_data(full_name=message.text.strip())
    await state.set_state(DriverRegister.car_info)
    await message.answer("Автомобиль (марка, модель, гос. номер):")


@router.message(DriverRegister.car_info, F.text)
async def reg_car(message: Message, state: FSMContext) -> None:
    await state.update_data(car_info=message.text.strip())
    await state.set_state(DriverRegister.phone)
    await message.answer("Ваш номер телефона:")


@router.message(DriverRegister.phone, F.text)
async def reg_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    import logging
    logger = logging.getLogger("taxi_bot.driver")
    try:
        data = await state.get_data()
        await state.clear()
        ensure_user(message.from_user, prefer_driver=True)
        u = User.get(telegram_id=message.from_user.id)
        dprof = DriverProfile.get(user=u)
        DriverProfile.update(
            full_name=data.get("full_name", ""),
            car_info=data.get("car_info", ""),
            phone=message.text.strip(),
            status=DriverStatus.PENDING.value,
        ).where(DriverProfile.id == dprof.id).execute()
        await message.answer(
            "✅ Анкета отправлена!\n\n"
            f"Имя: {data.get('full_name', '—')}\n"
            f"Авто: {data.get('car_info', '—')}\n"
            f"Телефон: {message.text.strip()}\n\n"
            "Ожидайте подтверждения администратора. Вам придёт уведомление.",
            reply_markup=keyboards.main_driver_kb(),
        )
        try:
            await notify_driver_registered(bot, data.get("full_name", ""), message.from_user.id)
        except Exception as e:
            logger.warning("Failed to notify admins about driver registration: %s", e)
    except Exception as e:
        logger.exception("Error in reg_phone handler")
        await state.clear()
        await message.answer(
            "Произошла ошибка при регистрации. Попробуйте снова нажать «🟢 Онлайн».",
            reply_markup=keyboards.main_driver_kb(),
        )

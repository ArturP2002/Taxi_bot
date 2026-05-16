import io
from datetime import datetime, timezone

import qrcode
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from app.bot import keyboards
from app.bot.states import PassengerOrder, RelayChat
from app.bot.users import ensure_user
from app.models import Direction, Order, OrderStatus
from app.services import code_service, order_service
from app.services.admin_notify import notify_new_order

router = Router(name="passenger")


@router.message(F.text == "🚕 Заказать поездку")
@router.message(Command("order"))
async def start_order(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user)
    directions = list(Direction.select().where(Direction.enabled == True))  # noqa: E712
    if not directions:
        await message.answer("Направления пока недоступны.")
        return
    await state.set_state(PassengerOrder.choosing_direction)
    await message.answer("Выберите направление:", reply_markup=keyboards.directions_inline(directions))


@router.callback_query(PassengerOrder.choosing_direction, F.data.startswith("dir:"))
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
    if message.text not in {str(i) for i in range(1, 7)}:
        await message.answer("Выберите 1–6.")
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
    order = Order.create(
        direction=direction,
        passenger=user,
        from_location=data["from_location"],
        to_location=data["to_location"],
        seats=data["seats"],
        phone=message.text.strip(),
        status=OrderStatus.NEW.value,
        confirmation_code_hash="tmp",
        code_issued_at=now,
    )
    Order.update(confirmation_code_hash=code_service.hash_code(order.id, code)).where(Order.id == order.id).execute()
    order = Order.get_by_id(order.id)
    token = code_service.build_qr_token(order.id)

    suggestion = order_service.suggest_driver_for_order(order)

    qr = qrcode.make(token)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)
    caption = (
        f"✅ Заявка #{order.id} создана!\n"
        f"Направление: {direction.from_label} → {direction.to_label}\n"
        f"Откуда: {order.from_location}\n"
        f"Куда: {order.to_location}\n"
        f"Мест: {order.seats}\n"
        f"Код: {code}\n\n"
        "Назовите код водителю при посадке или покажите QR. Код одноразовый.\n"
        "После назначения водителя можно писать в «Связь» внутри поездки."
    )
    await message.answer_photo(
        BufferedInputFile(buf.read(), filename="qr.png"),
        caption=caption,
        reply_markup=keyboards.main_passenger_kb(),
    )

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


@router.message(F.text == "📞 Связь")
@router.message(Command("contact"))
async def contact_menu(message: Message, state: FSMContext) -> None:
    user = ensure_user(message.from_user)
    orders = Order.select().where(
        (Order.passenger_id == user.id) & (Order.status == OrderStatus.IN_PROGRESS.value)
    )
    rows = list(orders)
    if not rows:
        await message.answer("Нет активной поездки для связи.")
        return
    o = rows[0]
    await state.set_state(RelayChat.active)
    await state.update_data(relay_order_id=o.id)
    await message.answer(
        f"Чат по заказу #{o.id}. Пишите сообщения — их увидит водитель. /stop чтобы выйти."
    )


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
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
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

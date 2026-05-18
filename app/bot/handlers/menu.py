from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards
from app.bot.messages import PASSENGER_RULES, DRIVER_RULES
from app.bot.users import ensure_user
from app.bot.handlers.passenger import continue_start_order

router = Router(name="menu")


@router.message(F.text == keyboards.BTN_ORDER_RIDE)
@router.message(Command("order"))
async def menu_order_ride(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user)
    await message.answer(PASSENGER_RULES)
    await continue_start_order(message, state)


@router.message(F.text == keyboards.BTN_DRIVER_MODE)
@router.message(Command("driver"))
async def menu_driver(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user, prefer_driver=True)
    await message.answer(DRIVER_RULES, reply_markup=keyboards.main_driver_kb())


@router.message(F.text == keyboards.BTN_PASSENGER_MODE)
@router.message(Command("passenger"))
async def menu_passenger(message: Message, state: FSMContext) -> None:
    await state.clear()
    ensure_user(message.from_user)
    await message.answer(PASSENGER_RULES, reply_markup=keyboards.main_passenger_kb())

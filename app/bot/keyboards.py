from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder


def main_passenger_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="🚕 Заказать поездку")
    b.button(text="📞 Связь")
    b.button(text="🧑‍✈️ Я водитель")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def main_driver_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="🟢 Онлайн")
    b.button(text="🔴 Оффлайн")
    b.button(text="📥 Мой заказ")
    b.button(text="💰 Баланс")
    b.button(text="📊 История")
    b.button(text="💸 Оплатить долг")
    b.button(text="🔍 Проверить платёж")
    b.button(text="➕ Предложить маршрут")
    b.button(text="👤 Режим пассажира")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def seats_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for i in range(1, 7):
        b.button(text=str(i))
    b.adjust(3)
    return b.as_markup(resize_keyboard=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="❌ Отмена")
    return b.as_markup(resize_keyboard=True)


def directions_inline(directions: list) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for d in directions:
        label = f"{d.from_label} → {d.to_label}"
        ib.button(text=label[:60], callback_data=f"dir:{d.id}")
    ib.adjust(1)
    return ib.as_markup()


def assignment_inline(assignment_id: int) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="✅ Принять", callback_data=f"acc:{assignment_id}")
    ib.button(text="❌ Отказ", callback_data=f"dec:{assignment_id}")
    ib.adjust(2)
    return ib.as_markup()


def admin_suggestion_inline(assignment_id: int) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="✅ Подтвердить водителя", callback_data=f"adm_ok:{assignment_id}")
    ib.button(text="❌ Другой водитель", callback_data=f"adm_no:{assignment_id}")
    ib.adjust(1)
    return ib.as_markup()


def trip_actions_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="🔁 Встать обратно")
    b.button(text="💬 Связь с пассажиром")
    b.button(text="✅ Завершить поездку")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)

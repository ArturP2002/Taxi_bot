from typing import List, Optional

from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from app.config import get_settings

BTN_ORDER_RIDE = "🚕 Заказать поездку"
BTN_DRIVER_MODE = "🧑‍✈️ Я водитель"
BTN_PASSENGER_MODE = "👤 Режим пассажира"
BTN_CANCEL = "❌ Отмена"

SEATS_OWN_MIN = 0
SEATS_OWN_MAX = 8
SEATS_ORDER_MIN = 1
SEATS_ORDER_MAX = 8
SEATS_VEHICLE_MIN = 1
SEATS_VEHICLE_MAX = 8

# Reply-keyboard labels: must not be captured by FSM text steps (registration, propose route, etc.)
DRIVER_MENU_TEXTS: frozenset[str] = frozenset({
    "🟢 Онлайн",
    "🔴 Оффлайн",
    "📥 Мой заказ",
    "👥 Мои пассажиры",
    "💰 Баланс",
    "📊 История",
    "ℹ️ Как считается долг",
    "💸 Оплатить долг",
    "🔍 Проверить платёж",
    "➕ Предложить маршрут",
    "😴 Отдых",
    "📞 Связь с админом",
    BTN_PASSENGER_MODE,
    "▶️ Старт поездки",
    "💬 Связь с пассажиром",
    "🔁 Встать обратно",
    "✅ Завершить поездку",
    BTN_CANCEL,
    BTN_ORDER_RIDE,
    BTN_DRIVER_MODE,
    "📞 Связь с водителем",
})


def main_passenger_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_ORDER_RIDE)
    b.button(text="📞 Связь с водителем")
    b.button(text="📞 Связь с админом")
    b.button(text=BTN_DRIVER_MODE)
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def main_driver_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="🟢 Онлайн")
    b.button(text="🔴 Оффлайн")
    b.button(text="📥 Мой заказ")
    b.button(text="👥 Мои пассажиры")
    b.button(text="💰 Баланс")
    b.button(text="📊 История")
    b.button(text="ℹ️ Как считается долг")
    b.button(text="💸 Оплатить долг")
    b.button(text="🔍 Проверить платёж")
    b.button(text="➕ Предложить маршрут")
    b.button(text="😴 Отдых")
    b.button(text="📞 Связь с админом")
    b.button(text=BTN_PASSENGER_MODE)
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def seats_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for i in range(SEATS_ORDER_MIN, SEATS_ORDER_MAX + 1):
        b.button(text=str(i))
    b.adjust(3)
    return b.as_markup(resize_keyboard=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_CANCEL)
    return b.as_markup(resize_keyboard=True)


def directions_inline(
    directions: list,
    *,
    page: int = 0,
    total_pages: int = 1,
    mode: str = "browse",
) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    for d in directions:
        label = f"{d.from_label} → {d.to_label}"
        ib.button(text=label[:60], callback_data=f"dirpick:{d.id}")
    ib.adjust(1)
    return _directions_nav(ib, page=page, total_pages=total_pages, mode=mode)


def direction_groups_inline(
    groups: list,
    *,
    page: int = 0,
    total_pages: int = 1,
    mode: str = "browse",
) -> InlineKeyboardMarkup:
    """Show туда/обратно pairs adjacent (↩ on return leg)."""
    ib = InlineKeyboardBuilder()
    for g in groups:
        d = g.forward
        label = f"{d.from_label} → {d.to_label}"
        ib.button(text=label[:58], callback_data=f"dirpick:{d.id}")
        if g.reverse:
            r = g.reverse
            ib.button(text=f"↩ {r.from_label} → {r.to_label}"[:58], callback_data=f"dirpick:{r.id}")
    ib.adjust(1)
    return _directions_nav(ib, page=page, total_pages=total_pages, mode=mode)


def _directions_nav(
    ib: InlineKeyboardBuilder, *, page: int, total_pages: int, mode: str
) -> InlineKeyboardMarkup:
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀ Назад", callback_data=f"dirpage:{page - 1}:{mode}")
    if page < total_pages - 1:
        nav.button(text="Вперёд ▶", callback_data=f"dirpage:{page + 1}:{mode}")
    nav.button(text="🔍 Поиск", callback_data="dirsearch")
    nav.adjust(2, 1)
    ib.attach(nav)
    return ib.as_markup()


def return_route_kb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="✅ Да, еду обратно", callback_data="return_yes")
    ib.button(text="❌ Нет, только туда", callback_data="return_no")
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


def before_trip_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="▶️ Старт поездки")
    b.button(text="💬 Связь с пассажиром")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def trip_actions_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="🔁 Встать обратно")
    b.button(text="💬 Связь с пассажиром")
    b.button(text="✅ Завершить поездку")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def passenger_pay_inline(order_id: int) -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="💳 Оплатить онлайн", callback_data=f"pay:{order_id}")
    ib.button(text="🔍 Проверить оплату", callback_data=f"paycheck:{order_id}")
    ib.adjust(1)
    return ib.as_markup()


def online_own_seats_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for i in range(SEATS_OWN_MIN, SEATS_OWN_MAX + 1):
        b.button(text=str(i))
    b.adjust(3)
    return b.as_markup(resize_keyboard=True)

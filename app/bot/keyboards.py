from datetime import date
from typing import List, Optional, Set

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from app.config import get_settings

BTN_ORDER_RIDE = "🚕 Заказать поездку"
BTN_DRIVER_MODE = "🧑‍✈️ Я водитель"
BTN_PASSENGER_MODE = "👤 Режим пассажира"
BTN_PASSENGER_CABINET = "👤 Личный кабинет"
BTN_BACK = "⬅️ Назад"
BTN_CANCEL = "❌ Отмена"

SEATS_OWN_MIN = 0
SEATS_OWN_MAX = 8
SEATS_ORDER_MIN = 1
SEATS_ORDER_MAX = 8
SEATS_VEHICLE_MIN = 1
SEATS_VEHICLE_MAX = 8

BTN_BOARDING_CODE = "🔐 Код и QR"

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
    "🧭 Направление",
    "📞 Связь с админом",
    BTN_PASSENGER_MODE,
    BTN_BACK,
    "▶️ Старт поездки",
    "📲 Посадка (код/QR)",
    "🚗 Выехать",
    "💬 Связь с пассажиром",
    "🔁 Встать обратно",
    "✅ Завершить поездку",
    "🔄 Передать пассажира админу",
    BTN_CANCEL,
    BTN_ORDER_RIDE,
    BTN_DRIVER_MODE,
    "📞 Связь с водителем",
    BTN_BOARDING_CODE,
})


def main_passenger_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_ORDER_RIDE)
    b.button(text=BTN_PASSENGER_CABINET)
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
    b.button(text="🧭 Направление")
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


def cancel_back_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_BACK)
    b.button(text=BTN_CANCEL)
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def confirm_edit_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="✅ Подтвердить")
    b.button(text=BTN_BACK)
    b.button(text=BTN_CANCEL)
    b.adjust(1)
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
    b.button(text="📲 Посадка (код/QR)")
    b.button(text="🚗 Выехать")
    b.button(text="💬 Связь с пассажиром")
    b.button(text="🔄 Передать пассажира админу")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def skip_salon_extra_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="⏭️ Без второго фото салона")
    b.button(text=BTN_CANCEL)
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def loading_photos_done_kb() -> InlineKeyboardMarkup:
    ib = InlineKeyboardBuilder()
    ib.button(text="✅ Фото готовы — на загрузку", callback_data="loading_photos_done")
    ib.adjust(1)
    return ib.as_markup()


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


def driver_offer_consent_kb(agreed: bool, offer_url: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if offer_url.strip():
        rows.append([
            InlineKeyboardButton(text="📄 Открыть оферту", url=offer_url.strip())
        ])
    label = "☑ Согласен" if agreed else "Согласен"
    rows.append([InlineKeyboardButton(text=label, callback_data="offer_toggle")])
    rows.append([InlineKeyboardButton(text="Продолжить", callback_data="offer_continue")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def contact_user_inline(
    telegram_id: int,
    label: str = "💬 Написать",
    *,
    username: str | None = None,
) -> InlineKeyboardMarkup:
    from app.util.telegram_links import telegram_open_url

    ib = InlineKeyboardBuilder()
    ib.button(text=label, url=telegram_open_url(telegram_id=telegram_id, username=username))
    return ib.as_markup()


def contact_admins_inline(admin_ids: frozenset[int]) -> InlineKeyboardMarkup:
    from app.util.telegram_links import user_chat_url

    ib = InlineKeyboardBuilder()
    for i, tid in enumerate(sorted(admin_ids)):
        ib.button(text=f"💬 Админ {i + 1}", url=user_chat_url(tid))
    ib.adjust(1)
    return ib.as_markup()


def trip_calendar_kb(
    year: int,
    month: int,
    *,
    available_dates: Set[date],
    direction_id: int,
) -> InlineKeyboardMarkup:
    """Passenger trip mode picker (ASAP vs custom date-time)."""
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton(
            text="⚡ Как можно скорее (без даты)",
            callback_data=f"tcal:asap:{direction_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="📝 Указать свою дату и время",
            callback_data=f"tcal:custom:{direction_id}",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scheduled_trips_pick_kb(trips: list) -> InlineKeyboardMarkup:
    from app.util.time_format import format_datetime_display

    rows: list[list[InlineKeyboardButton]] = []
    for t in trips:
        when = format_datetime_display(t.departure_at)
        free = max(0, int(t.seats_total) - int(t.seats_booked))
        rows.append([
            InlineKeyboardButton(
                text=f"🕐 {when} · мест {free}",
                callback_data=f"tcal:trip:{t.id}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def online_own_seats_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for i in range(SEATS_OWN_MIN, SEATS_OWN_MAX + 1):
        b.button(text=str(i))
    b.adjust(3)
    return b.as_markup(resize_keyboard=True)

from decimal import Decimal
from typing import Optional

from aiogram.types import Message

from app.models import Direction, Order

PASSENGER_RULES = """📌 ПРАВИЛА ПАССАЖИРА

— После оформления заказа вы получаете код и QR.
— Код подтверждает ваш заказ и количество мест.
— Покажите QR или назовите код водителю при посадке.
— Без кода или QR поездка не подтверждается.
— Один код действует только для одного заказа.
— Передавать код другим запрещено.
— После подтверждения поездки код становится недействительным.

❗ Если пассажир не предъявил код или QR при посадке, сервис не несёт ответственности за поездку, оплату и возможные спорные ситуации.

Спасибо за использование сервиса 🚕"""

DRIVER_RULES = """📌 ПРАВИЛА ВОДИТЕЛЯ

— Водитель работает только через систему сервиса.
— Перед началом поездки водитель обязан проверить код или QR пассажира.
— Без подтверждения кода поездка считается неоформленной.
— Один код подтверждает весь заказ и количество мест.
— После подтверждения поездки код становится недействительным.
— Водителю запрещено брать пассажиров "мимо системы".
— Водитель обязан соблюдать очередь направления и правила сервиса.
— При отказе от заказа или отсутствии ответа система может ограничить получение новых заказов.
— После завершения поездки водитель обязан подтвердить завершение в системе.
— Комиссия сервиса начисляется автоматически после завершения поездки.

❗ Если водитель начал поездку без подтверждения кода или QR, сервис не несёт ответственности за оплату, пассажиров и спорные ситуации.

Спасибо за сотрудничество 🚕"""

DRIVER_WELCOME_AFTER_APPROVAL = """🚗 ПОЧЕМУ ВОДИТЕЛЮ ВЫГОДНО РАБОТАТЬ С НАМИ

✅ Нет толкучки и хаоса
Каждое направление имеет свою очередь водителей.
Система распределяет заказы честно и по порядку.

✅ Нет войны за пассажира
Не нужно стоять и “ловить” клиентов.
Заказы приходят через систему.

✅ Честная очередь
После поездки водитель становится в конец очереди.
Все водители видят понятные правила работы.

✅ Меньше пустых поездок
Есть возможность автоматически встать в обратное направление после завершения рейса.

✅ Безопасность
Каждый пассажир имеет код или QR.
Без кода поездка не подтверждается.

✅ Удобная работа
Все заказы приходят прямо в Telegram.
Не нужны звонки, группы и постоянные переписки.

✅ Быстрый старт
Анкета заполняется только один раз.
После одобрения можно сразу выходить на линию.

✅ Контроль мест
Система учитывает количество свободных мест в машине.
Можно добирать пассажиров без путаницы.

✅ Прозрачные условия
Комиссия фиксированная и понятная.
Без скрытых платежей и неожиданных списаний.

✅ Поддержка
Администратор помогает решать спорные ситуации и контролирует порядок в системе.

🚕 Наша цель — сделать межгород удобным, честным и выгодным для водителей и пассажиров.
Приветствуем вы в нашей команде"""

DRIVER_LAUNCH_MESSAGE = """📢 ВАЖНО ДЛЯ ВОДИТЕЛЕЙ

Система находится на этапе активного запуска и набора постоянного потока клиентов.

Сейчас бот только начинает работу, поэтому в некоторых направлениях количество заказов может быть нестабильным. Но именно первые водители получают главное преимущество:

✅ закрепление в очереди
✅ приоритет в новых направлениях
✅ ранний доступ к постоянному потоку
✅ возможность первыми занять выгодные маршруты
✅ участие в развитии системы с самого начала

Мы строим не обычный чат, а полноценную систему межгородних перевозок:

— очередь
— обратные рейсы
— контроль мест
— код подтверждения
— меньше хаоса и толкучки
— честное распределение заказов

🚕 Наша цель — создать стабильный поток поездок и долгосрочную систему для водителей и пассажиров.

Спасибо, что развиваете систему вместе с нами."""

PASSENGER_OVERFLOW_MSG = (
    "⚠️ Мест в заявке больше, чем в ближайшей машине.\n"
    "Администратор подберёт другую машину или время.\n"
    "Не садитесь в машину без подтверждения в боте."
)

DRIVER_OVERFLOW_MSG = "⚠️ Заявка #{order_id} не влезает в вашу машину. Ожидайте решения администратора."

PASSENGER_BOARDING_CHECKLIST = """📋 Посадка — 3 шага:
1️⃣ Приезжайте к месту подачи в указанное время.
2️⃣ Покажите водителю QR или назовите 6-значный код из бота.
3️⃣ При вопросах — «📞 Связь с водителем» или «📞 Связь с админом»."""


def format_fare_line(direction: Direction, seats: int) -> str:
    per = Decimal(str(direction.price_per_seat)) * seats
    fixed = Decimal(str(direction.fixed_price))
    total = per + fixed
    parts = [f"Мест в заказе: {seats}"]
    if direction.price_per_seat and Decimal(str(direction.price_per_seat)) > 0:
        parts.append(f"Цена за место: {direction.price_per_seat} ₽")
    if fixed > 0:
        parts.append(f"Фикс за рейс: {fixed} ₽")
    parts.append(f"Ориентир суммы: {total} ₽")
    return "\n".join(parts)


def format_order_summary(
    order: Order,
    direction: Direction,
    *,
    driver_name: Optional[str] = None,
    extra: Optional[str] = None,
) -> str:
    seats = order.seats
    plat = order.platform_seats if order.platform_seats is not None else seats
    lines = [
        f"Заказ #{order.id}",
        f"📍 {direction.from_label} → {direction.to_label}",
        f"Откуда: {order.from_location}",
        f"Куда: {order.to_location}",
        format_fare_line(direction, plat if plat else seats),
    ]
    if plat != seats:
        lines.append(f"(в машине учтено мест: {plat})")
    if order.pickup_location or order.pickup_time_text:
        lines.append(
            f"Подача: {order.pickup_location or '—'} {order.pickup_time_text or ''}".strip()
        )
    if driver_name:
        lines.append(f"Водитель: {driver_name}")
        if order.status == "assigned":
            lines.append(f"Авто: —")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def format_passenger_loading_update(
    *,
    order: Order,
    direction: Direction,
    driver_name: str,
    car_info: Optional[str],
    status_label: str,
    eta_label: Optional[str],
) -> str:
    base = format_order_summary(order, direction, driver_name=driver_name)
    lines = ["🚐 Статус загрузки:", base, status_label]
    if car_info:
        lines.append(f"Авто: {car_info}")
    if eta_label:
        lines.append(f"⏱ Ориентир: {eta_label}")
    lines.append("Дождитесь подтверждения перед посадкой, если админ пересадит — придёт новое сообщение.")
    return "\n".join(lines)


def format_driver_loading_status(
    *,
    route: str,
    status_label: str,
    occupied: int,
    max_seats: int,
    passengers_block: str,
) -> str:
    return (
        f"🟡 Вы на загрузке: {route}\n"
        f"{status_label}\n"
        f"Занято: {occupied}/{max_seats}\n\n"
        f"Пассажиры:\n{passengers_block}\n\n"
        "После посадки всех — «▶️ Старт поездки» и код/QR пассажира."
    )


def format_queue_driver_loading_notice(
    *,
    loader_name: str,
    route: str,
    position: int,
    loading_label: Optional[str],
) -> str:
    text = (
        f"ℹ️ {loader_name} на загрузке по маршруту {route}.\n"
        f"Вы №{position} в очереди."
    )
    if loading_label:
        text += f"\n⏱ Ваша загрузка: {loading_label}"
    else:
        text += "\nОжидайте."
    return text


def format_driver_on_loading_accept(
    *,
    route: str,
    pickup_hint: str,
) -> str:
    return (
        f"🟡 Вы на загрузке по маршруту {route}.\n"
        f"Подача: {pickup_hint}\n"
        "Сначала пришлите фото машины (кузов/салон), затем пассажиры получат уведомление."
    )


async def send_passenger_rules(message: Message, **kwargs) -> None:
    await message.answer(PASSENGER_RULES, **kwargs)


async def send_driver_rules(message: Message, **kwargs) -> None:
    await message.answer(DRIVER_RULES, **kwargs)

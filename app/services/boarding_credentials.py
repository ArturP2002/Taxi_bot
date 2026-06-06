"""Send passenger boarding code + QR; recover code from DB for resend."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.models import Direction, Order, OrderStatus
from app.services import code_service
from app.util.datetimeutil import utcnow

logger = logging.getLogger("taxi_bot.boarding_credentials")

_bot_username_cache: Optional[str] = None


async def get_bot_username(bot: Bot) -> str:
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        me = await bot.get_me()
        _bot_username_cache = (me.username or "").strip()
    except Exception as e:
        logger.warning("get_me failed: %s", e)
        _bot_username_cache = ""
    return _bot_username_cache


def boarding_code_for_order(order: Order) -> Optional[str]:
    raw = getattr(order, "boarding_code", None) or ""
    return code_service.normalize_boarding_code(str(raw)) if raw else None


def format_code_message(order: Order, direction: Direction, code: str) -> str:
    return (
        f"🔐 КОД ПОСАДКИ · заказ #{order.id}\n"
        f"📍 {direction.from_label} → {direction.to_label}\n\n"
        f"      {code}\n\n"
        "Назовите эти 6 цифр водителю или покажите QR ниже.\n"
        "Код действует до начала поездки."
    )


def _order_total_price(order: Order, direction: Direction) -> Decimal:
    seats = order.platform_seats if order.platform_seats is not None else order.seats
    per = Decimal(str(direction.price_per_seat)) * seats
    fixed = Decimal(str(direction.fixed_price))
    pickup = Decimal(str(order.pickup_surcharge or 0))
    dropoff = Decimal(str(getattr(order, "dropoff_surcharge", None) or 0))
    return per + fixed + pickup + dropoff


def _departure_arrival_labels(
    order: Order,
    direction: Direction,
) -> tuple[str, str]:
    from app.util.time_format import format_departure_label, format_datetime_display

    dep_dt: Optional[datetime] = None
    if order.scheduled_trip_id:
        from app.models.scheduled_trip import ScheduledTrip

        try:
            trip = ScheduledTrip.get_by_id(order.scheduled_trip_id)
            dep_dt = trip.departure_at
            if dep_dt and dep_dt.tzinfo is None:
                dep_dt = dep_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    if dep_dt is None and order.requested_departure_at:
        dep_dt = order.requested_departure_at
        if dep_dt.tzinfo is None:
            dep_dt = dep_dt.replace(tzinfo=timezone.utc)

    if dep_dt is None:
        return "согласуется с оператором", "согласуется с оператором"

    dep_label = format_departure_label(dep_dt)
    eta_min = int(direction.estimated_time_min or 0)
    if eta_min > 0 and dep_dt.hour != 0:
        arr_dt = dep_dt + timedelta(minutes=eta_min)
        arr_label = format_datetime_display(arr_dt)
    elif eta_min > 0:
        arr_label = f"{format_departure_label(dep_dt)} + {eta_min // 60 or 1} ч (ориентир)"
    else:
        arr_label = "согласуется с оператором"
    return dep_label, arr_label


def format_passenger_trip_ticket(
    order: Order,
    direction: Direction,
    *,
    driver_name: Optional[str] = None,
    car_info: Optional[str] = None,
    code: str,
) -> str:
    from app.bot import messages as bot_messages

    he = bot_messages.html_escape
    dep_label, arr_label = _departure_arrival_labels(order, direction)
    total = _order_total_price(order, direction)
    lines = [
        f"🎫 Поездка подтверждена · заказ #{order.id}",
        f"📍 {he(direction.from_label)} → {he(direction.to_label)}",
        f"Место отъезда: {he(order.from_location)}",
        f"Место прибытия: {he(order.to_location)}",
        f"Мест: {order.seats}",
        f"Отправление: {he(dep_label)}",
        f"Прибытие: {he(arr_label)}",
    ]
    if driver_name:
        lines.append(f"Водитель: {he(driver_name)}")
    if car_info:
        lines.append(f"Авто: {he(car_info)}")
    lines.append(f"Стоимость: {total} ₽")
    if getattr(order, "wants_pickup", False):
        surcharge = Decimal(str(order.pickup_surcharge or 0))
        note = f" (+ {surcharge} ₽)" if surcharge > 0 else " (цена уточняется оператором)"
        lines.append(f"Доп.услуга «Забрать меня»{he(note)}")
    if getattr(order, "wants_dropoff", False):
        surcharge = Decimal(str(getattr(order, "dropoff_surcharge", None) or 0))
        note = f" (+ {surcharge} ₽)" if surcharge > 0 else " (цена уточняется оператором)"
        lines.append(f"Доп.услуга «Довезти до места»{he(note)}")
    lines.append("")
    lines.append(f"🔐 Код посадки: {he(code)}")
    lines.append("")
    lines.append(bot_messages.passenger_rules_html())
    return "\n".join(lines)


async def send_passenger_boarding_credentials(
    bot: Bot,
    order: Order,
    *,
    code: Optional[str] = None,
    direction: Optional[Direction] = None,
) -> bool:
    """
    Send prominent 6-digit code + scannable QR.
    Returns False if code unknown (legacy order without boarding_code).
    """
    order = Order.get_by_id(order.id)
    if order.status in (
        OrderStatus.COMPLETED.value,
        OrderStatus.CANCELLED.value,
        OrderStatus.IN_PROGRESS.value,
    ):
        return False
    if order.code_consumed_at:
        return False

    code = code or boarding_code_for_order(order)
    if not code:
        return False

    direction = direction or Direction.get_by_id(order.direction_id)
    username = await get_bot_username(bot)
    qr_payload = code_service.build_telegram_deeplink(username, order.id, code)
    png = code_service.render_qr_png(qr_payload)

    await bot.send_message(
        order.passenger.telegram_id,
        format_code_message(order, direction, code),
    )
    caption = (
        f"QR для заказа #{order.id}\n"
        "Водитель может отсканировать камерой (откроется бот) "
        "или вы сфотографируете QR для «📷 Сканировать QR»."
    )
    await bot.send_photo(
        order.passenger.telegram_id,
        BufferedInputFile(png, filename=f"order_{order.id}_qr.png"),
        caption=caption,
    )
    return True


async def send_passenger_trip_ticket(
    bot: Bot,
    order: Order,
    *,
    driver=None,
    direction: Optional[Direction] = None,
    code: Optional[str] = None,
    force: bool = False,
) -> bool:
    """Single idempotent passenger e-ticket with photo, price, driver info, QR, rules."""
    order = Order.get_by_id(order.id)
    if order.passenger_ticket_sent_at and not force:
        return False
    if order.status in (OrderStatus.CANCELLED.value, OrderStatus.COMPLETED.value):
        return False

    direction = direction or Direction.get_by_id(order.direction_id)

    driver_name = None
    car_info = None
    driver_id = None
    if driver is not None:
        driver_name = getattr(driver, "full_name", None)
        car_info = getattr(driver, "car_info", None)
        driver_id = getattr(driver, "id", None)
    else:
        from app.models import AssignmentStatus, OrderDriverAssignment, DriverProfile

        ass = (
            OrderDriverAssignment.select()
            .where(
                (OrderDriverAssignment.order_id == order.id)
                & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            )
            .order_by(OrderDriverAssignment.assigned_at.desc())
            .first()
        )
        if ass:
            try:
                drv = DriverProfile.get_by_id(ass.driver_id)
                driver_name = drv.full_name
                car_info = drv.car_info
                driver_id = drv.id
            except Exception:
                pass

    if not driver_id:
        logger.info("trip ticket skipped for order %s: no assigned driver", order.id)
        return False

    code = code or boarding_code_for_order(order)
    if not code:
        code = code_service.generate_six_digit_code()
        code_service.persist_boarding_code(order.id, code)

    ticket_text = format_passenger_trip_ticket(
        order,
        direction,
        driver_name=driver_name,
        car_info=car_info,
        code=code,
    )

    photo_sent = False
    if driver_id:
        from app.services.photo_service import car_photo_file_ids_for_driver

        file_ids = car_photo_file_ids_for_driver(driver_id)
        if file_ids:
            try:
                from app.bot.messages import TELEGRAM_HTML

                await bot.send_photo(
                    order.passenger.telegram_id,
                    file_ids[0],
                    caption=ticket_text[:1024],
                    parse_mode=TELEGRAM_HTML,
                )
                photo_sent = True
            except Exception as e:
                logger.warning("trip ticket photo for order %s: %s", order.id, e)

    if not photo_sent:
        try:
            from app.bot.messages import TELEGRAM_HTML

            await bot.send_message(
                order.passenger.telegram_id,
                ticket_text,
                parse_mode=TELEGRAM_HTML,
            )
        except Exception as e:
            logger.warning("trip ticket message for order %s: %s", order.id, e)
            return False

    username = await get_bot_username(bot)
    qr_payload = code_service.build_telegram_deeplink(username, order.id, code)
    png = code_service.render_qr_png(qr_payload)
    qr_caption = (
        f"🔐 QR для посадки · заказ #{order.id}\n"
        "Покажите водителю при посадке."
    )
    try:
        await bot.send_photo(
            order.passenger.telegram_id,
            BufferedInputFile(png, filename=f"order_{order.id}_qr.png"),
            caption=qr_caption,
        )
    except Exception as e:
        logger.warning("trip ticket QR for order %s: %s", order.id, e)
        return False

    now = utcnow()
    Order.update(
        passenger_ticket_sent_at=now,
        code_issued_at=now,
        updated_at=now,
    ).where(Order.id == order.id).execute()
    return True


async def notify_passenger_driver_assigned(
    bot: Bot,
    order: Order,
    driver,
    direction: Direction,
) -> None:
    try:
        await send_passenger_trip_ticket(bot, order, driver=driver, direction=direction)
    except Exception as e:
        logger.warning("trip ticket for order %s: %s", order.id, e)


async def issue_and_send_new_credentials(bot: Bot, order: Order) -> str:
    """Regenerate code (invalidates old QR) and send to passenger."""
    code = code_service.generate_six_digit_code()
    code_service.persist_boarding_code(order.id, code)
    Order.update(passenger_ticket_sent_at=None).where(Order.id == order.id).execute()
    order = Order.get_by_id(order.id)
    from app.models import AssignmentStatus, OrderDriverAssignment, DriverProfile

    driver = None
    ass = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == order.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
        )
        .first()
    )
    if ass:
        try:
            driver = DriverProfile.get_by_id(ass.driver_id)
        except Exception:
            pass
    if driver:
        await send_passenger_trip_ticket(bot, order, driver=driver, code=code, force=True)
    else:
        await send_passenger_boarding_credentials(bot, order, code=code)
    return code

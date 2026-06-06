"""Preview summaries and edit-field metadata for passenger orders and driver registration."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.models import Direction


def _format_departure(data: dict[str, Any]) -> str:
    iso = data.get("requested_departure_at")
    if not iso:
        return "Как можно скорее"
    try:
        dep = datetime.fromisoformat(str(iso))
        if dep.tzinfo is None:
            dep = dep.replace(tzinfo=timezone.utc)
        from app.util.time_format import format_departure_label

        return format_departure_label(dep)
    except Exception:
        return str(iso)


def format_passenger_order_preview(data: dict[str, Any], direction: Direction) -> str:
    extras: list[str] = []
    if data.get("wants_pickup"):
        extras.append("Забрать меня")
    if data.get("wants_dropoff"):
        extras.append("Довезти до места")
    extras_txt = ", ".join(extras) if extras else "нет"

    return (
        "📋 Предпросмотр заявки\n\n"
        f"Маршрут: {direction.from_label} → {direction.to_label}\n"
        f"Место отъезда: {data.get('from_location', direction.from_label)}\n"
        f"Место прибытия: {data.get('to_location', direction.to_label)}\n"
        f"Дата: {_format_departure(data)}\n"
        f"Мест: {data.get('seats', '—')}\n"
        f"Телефон: {data.get('phone', '—')}\n"
        f"Доп.услуги: {extras_txt}"
    )


PASSENGER_PREVIEW_EDIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("direction", "Маршрут"),
    ("extras", "Доп.услуги"),
    ("date", "Дата выезда"),
    ("seats", "Количество мест"),
    ("phone", "Телефон"),
)


def format_driver_registration_preview(data: dict[str, Any]) -> str:
    route_from = data.get("route_from", "—")
    route_to = data.get("route_to", "—")
    include_return = data.get("include_return")
    if include_return is True:
        return_txt = f"да ({route_to} → {route_from})"
    elif include_return is False:
        return_txt = "нет"
    else:
        return_txt = "—"

    return (
        "📋 Предпросмотр анкеты\n\n"
        f"Маршрут: {route_from} → {route_to}\n"
        f"Обратный рейс: {return_txt}\n"
        f"ФИО: {data.get('full_name', '—')}\n"
        f"Авто: {data.get('car_info', '—')}\n"
        f"Телефон: {data.get('phone', '—')}\n"
        f"Мест в машине: {data.get('max_seats', '—')}\n"
        f"Тариф: {data.get('price_per_seat', '0')} ₽/место + "
        f"{data.get('fixed_price', '0')} ₽ фикс"
    )


DRIVER_PREVIEW_EDIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("route_from", "Город отправления"),
    ("route_to", "Город назначения"),
    ("return_route", "Обратный рейс"),
    ("full_name", "ФИО"),
    ("car_info", "Автомобиль"),
    ("photos", "Фото авто"),
    ("phone", "Телефон"),
    ("max_seats", "Мест в машине"),
    ("price_per_seat", "Цена за место"),
    ("fixed_price", "Фикс за рейс"),
)

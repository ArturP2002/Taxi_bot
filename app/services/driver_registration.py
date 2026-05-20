"""Driver onboarding: persist draft in DB (survives FSM loss / restarts) and notify admins."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional, Tuple

from aiogram import Bot

from app.bot import keyboards
from app.models import DriverProfile, DriverStatus
from app.services.admin_notify import notify_driver_registered, notify_proposal

logger = logging.getLogger("taxi_bot.driver_registration")


def parse_draft_route(dprof: DriverProfile) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
    route_from = (dprof.current_city or "").strip() or None
    route_to: Optional[str] = None
    include_return: Optional[bool] = None
    note = (dprof.tariff_note or "").strip()
    if not note:
        return route_from, route_to, include_return
    if "|" in note:
        to_part, flag = note.split("|", 1)
        route_to = to_part.strip() or None
        if flag in ("0", "1"):
            include_return = flag == "1"
    else:
        route_to = note
    return route_from, route_to, include_return


def save_draft_route_from(dprof: DriverProfile, route_from: str) -> None:
    dprof.current_city = route_from.strip()
    dprof.status = DriverStatus.PENDING.value
    dprof.save()


def save_draft_route_to(dprof: DriverProfile, route_to: str) -> None:
    dprof.tariff_note = route_to.strip()
    dprof.status = DriverStatus.PENDING.value
    dprof.save()


def save_draft_return_choice(dprof: DriverProfile, route_to: str, include_return: bool) -> None:
    dprof.tariff_note = f"{route_to.strip()}|{1 if include_return else 0}"
    dprof.status = DriverStatus.PENDING.value
    dprof.save()


def draft_route_label(dprof: DriverProfile) -> Optional[str]:
    route_from, route_to, _ = parse_draft_route(dprof)
    if route_from and route_to:
        return f"{route_from} → {route_to}"
    return route_from or route_to


def merge_registration_data(dprof: DriverProfile, fsm_data: dict[str, Any]) -> dict[str, Any]:
    route_from, route_to, include_return = parse_draft_route(dprof)
    merged = dict(fsm_data)
    if not merged.get("route_from") and route_from:
        merged["route_from"] = route_from
    if not merged.get("route_to") and route_to:
        merged["route_to"] = route_to
    if "include_return" not in merged and include_return is not None:
        merged["include_return"] = include_return
    if not merged.get("full_name"):
        merged["full_name"] = (dprof.full_name or "").strip()
    return merged


async def finalize_driver_registration(
    bot: Bot,
    *,
    dprof: DriverProfile,
    telegram_id: int,
    data: dict[str, Any],
) -> tuple[bool, str]:
    """Save profile, create route proposals, notify admins. Returns (ok, user_message)."""
    merged = merge_registration_data(dprof, data)
    full_name = (merged.get("full_name") or "").strip()
    if not full_name:
        return False, "ФИО не указано. Введите ФИО:"

    route_from = (merged.get("route_from") or "").strip()
    route_to = (merged.get("route_to") or "").strip()
    if not route_from or not route_to:
        return False, "route_lost"

    max_seats = int(merged.get("max_seats") or dprof.max_seats or keyboards.SEATS_VEHICLE_MAX)
    own_seats = int(merged.get("own_seats", dprof.own_seats_reserved or 0))
    if own_seats >= max_seats:
        return False, f"own_seats:{max_seats}"

    try:
        price = Decimal(str(merged.get("price_per_seat", dprof.proposed_price_per_seat or "0")))
        fixed = Decimal(str(merged.get("fixed_price", dprof.proposed_fixed_price or "0")))
    except Exception:
        return False, "price_invalid"

    include_return = bool(merged.get("include_return", True))
    car_info = (merged.get("car_info") or dprof.car_info or "").strip()
    phone = (merged.get("phone") or dprof.phone or "").strip()

    dprof.full_name = full_name
    dprof.car_info = car_info
    dprof.phone = phone
    dprof.max_seats = max_seats
    dprof.own_seats_reserved = own_seats
    dprof.proposed_price_per_seat = price
    dprof.proposed_fixed_price = fixed
    dprof.status = DriverStatus.PENDING.value
    dprof.save()

    try:
        await notify_driver_registered(
            bot,
            full_name,
            telegram_id,
            driver_id=dprof.id,
            route=f"{route_from} → {route_to}",
            max_seats=max_seats,
            tariff=f"{price}/{fixed}",
            car_info=car_info,
            phone=phone,
        )
    except Exception as e:
        logger.warning("Admin notify failed for driver %s: %s", dprof.id, e)

    proposal_error: Optional[str] = None
    try:
        from app.services import reserve_service

        reserve_service.create_reserved_paired_proposals(
            dprof,
            route_from,
            route_to,
            max_seats=max_seats,
            own_seats=own_seats,
            price_per_seat=price,
            fixed_price=fixed,
            comment=f"Анкета: {car_info}" if car_info else "Анкета водителя",
            include_return=include_return,
        )
        try:
            await notify_proposal(
                bot, route_from, route_to, full_name, paired=include_return,
            )
        except Exception as e:
            logger.warning("Proposal notify failed for driver %s: %s", dprof.id, e)
    except Exception as e:
        logger.exception("create_paired_proposals failed for driver %s: %s", dprof.id, e)
        proposal_error = str(e)

    from app.bot.messages import DRIVER_LAUNCH_MESSAGE
    from app.services.photo_service import send_registration_album_to_admins

    try:
        await bot.send_message(telegram_id, DRIVER_LAUNCH_MESSAGE)
    except Exception as e:
        logger.warning("Launch message failed for %s: %s", telegram_id, e)
    try:
        await send_registration_album_to_admins(
            bot,
            dprof.id,
            caption=f"👤 Фото авто: {full_name}\n{route_from} → {route_to}",
        )
    except Exception as e:
        logger.warning("Registration album failed: %s", e)

    route_txt = f"{route_from} → {route_to}"
    if include_return:
        route_txt += f"\n↩ {route_to} → {route_from}"
    msg = (
        "✅ Анкета отправлена!\n\n"
        f"Маршрут(ы): {route_txt}\n"
        f"ФИО: {full_name}\n"
        f"Авто: {car_info or '—'}\n"
        f"Тел: {phone or '—'}\n"
        f"Мест: {max_seats} (своих: {own_seats})\n"
        f"Тариф: {price} ₽/место + {fixed} ₽ фикс\n\n"
        "Ожидайте подтверждения. «📞 Связь с админом» — в любой момент."
    )
    if proposal_error:
        msg += (
            "\n\n⚠️ Маршрут в заявках мог не сохраниться — администратор увидит анкету "
            "во вкладке «Водители»."
        )
    return True, msg

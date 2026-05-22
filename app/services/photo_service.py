"""Driver registration and loading photo storage."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from aiogram import Bot
from aiogram.types import InputMediaPhoto

from app.models import DriverRegistrationPhoto, DriverProfile, LoadingPhoto


REG_PHOTO_KINDS = ("front", "back", "left", "right", "salon", "salon2")


def save_registration_photo(driver_id: int, kind: str, file_id: str, *, sort_order: int = 0) -> None:
    DriverRegistrationPhoto.delete().where(
        (DriverRegistrationPhoto.driver_id == driver_id)
        & (DriverRegistrationPhoto.kind == kind)
    ).execute()
    DriverRegistrationPhoto.create(
        driver_id=driver_id,
        kind=kind,
        file_id=file_id,
        sort_order=sort_order,
    )


def list_registration_photos(driver_id: int) -> List[DriverRegistrationPhoto]:
    order = {k: i for i, k in enumerate(REG_PHOTO_KINDS)}
    rows = list(DriverRegistrationPhoto.select().where(DriverRegistrationPhoto.driver_id == driver_id))
    rows.sort(key=lambda r: order.get(r.kind, 99))
    return rows


def new_loading_session_id() -> str:
    return uuid.uuid4().hex[:16]


def save_loading_photo(
    driver_id: int,
    direction_id: int,
    session_id: str,
    file_id: str,
) -> None:
    LoadingPhoto.create(
        driver_id=driver_id,
        direction_id=direction_id,
        session_id=session_id,
        file_id=file_id,
    )


def confirm_loading_photos(driver: DriverProfile) -> None:
    now = datetime.now(timezone.utc)
    DriverProfile.update(loading_photos_ok_at=now).where(DriverProfile.id == driver.id).execute()


def clear_loading_photos_session(driver_id: int, session_id: str) -> None:
    LoadingPhoto.delete().where(
        (LoadingPhoto.driver_id == driver_id) & (LoadingPhoto.session_id == session_id)
    ).execute()


def list_loading_session_photos(driver_id: int, session_id: str) -> List[LoadingPhoto]:
    return list(
        LoadingPhoto.select()
        .where(
            (LoadingPhoto.driver_id == driver_id)
            & (LoadingPhoto.session_id == session_id)
        )
        .order_by(LoadingPhoto.id)
    )


def car_photo_file_ids_for_driver(
    driver_id: int, *, session_id: Optional[str] = None
) -> List[str]:
    """Loading session photos first, else registration album (for passenger preview)."""
    if session_id:
        ids = [p.file_id for p in list_loading_session_photos(driver_id, session_id)]
        if ids:
            return ids[:10]
    return [p.file_id for p in list_registration_photos(driver_id)[:10]]


async def send_car_photos_to_passengers(
    bot: Bot,
    driver: DriverProfile,
    direction_id: int,
    *,
    session_id: Optional[str] = None,
    route_label: str = "",
) -> int:
    """
    Send vehicle photos to passengers on this driver's current loading trip.
    Returns number of passengers who received the album.
    """
    from app.models import AssignmentStatus, Order, OrderDriverAssignment, OrderStatus, User

    file_ids = car_photo_file_ids_for_driver(driver.id, session_id=session_id)
    if not file_ids:
        return 0

    cap = (
        f"🚗 Фото машины водителя {driver.full_name or ''}\n"
        f"📍 {route_label}\n"
        f"Авто: {driver.car_info or '—'}"
    ).strip()[:1024]

    orders = list(
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver.id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.direction_id == direction_id)
            & (Order.status == OrderStatus.ASSIGNED.value)
        )
        .order_by(Order.id)
    )
    seen_passengers: set[int] = set()
    sent = 0
    for o in orders:
        if o.passenger_id in seen_passengers:
            continue
        seen_passengers.add(o.passenger_id)
        try:
            pu = User.get_by_id(o.passenger_id)
            media: list[InputMediaPhoto] = []
            for i, fid in enumerate(file_ids):
                if i == 0:
                    media.append(
                        InputMediaPhoto(
                            media=fid,
                            caption=f"{cap}\n\nЗаказ #{o.id}",
                        )
                    )
                else:
                    media.append(InputMediaPhoto(media=fid))
            await bot.send_media_group(pu.telegram_id, media=media)
            sent += 1
        except Exception:
            try:
                await bot.send_message(
                    pu.telegram_id,
                    f"{cap}\n\n(фото не удалось отправить альбомом, обратитесь к админу)",
                )
                sent += 1
            except Exception:
                pass
    return sent


async def send_registration_album_to_admins(bot: Bot, driver_id: int, *, caption: str) -> None:
    from app.services.admin_notify import notify_admins

    photos = list_registration_photos(driver_id)
    if not photos:
        await notify_admins(bot, caption)
        return
    cap = caption[:1024]
    media: list[InputMediaPhoto] = []
    for i, p in enumerate(photos[:10]):
        if i == 0:
            media.append(InputMediaPhoto(media=p.file_id, caption=cap))
        else:
            media.append(InputMediaPhoto(media=p.file_id))
    from app.config import get_settings

    settings = get_settings()
    if not settings.admin_ids:
        return
    for admin_id in settings.admin_ids:
        try:
            await bot.send_media_group(admin_id, media=media)
        except Exception:
            try:
                await bot.send_message(admin_id, caption)
            except Exception:
                pass

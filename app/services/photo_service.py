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


async def send_registration_album_to_admins(bot: Bot, driver_id: int, *, caption: str) -> None:
    from app.services.admin_notify import notify_admins

    photos = list_registration_photos(driver_id)
    if not photos:
        await notify_admins(bot, caption)
        return
    media = [InputMediaPhoto(media=p.file_id) for p in photos[:10]]
    media[0].caption = caption[:1024]
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

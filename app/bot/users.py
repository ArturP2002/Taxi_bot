from aiogram.types import User as TgUser

from app.config import get_settings
from app.models import User, UserRole, PassengerProfile, DriverProfile, DriverStatus
from app.util.datetimeutil import utcnow


def is_admin(telegram_id: int) -> bool:
    return telegram_id in get_settings().admin_ids


def ensure_user(tg_user: TgUser, *, prefer_driver: bool = False) -> User:
    tid = tg_user.id
    role = UserRole.DRIVER.value if prefer_driver else UserRole.PASSENGER.value
    user, created = User.get_or_create(
        telegram_id=tid,
        defaults={
            "username": tg_user.username,
            "first_name": tg_user.first_name,
            "last_name": tg_user.last_name,
            "role": role,
            "last_seen_at": utcnow(),
        },
    )
    updates: dict = {"last_seen_at": utcnow()}
    if tg_user.username and user.username != tg_user.username:
        updates["username"] = tg_user.username
    if tg_user.first_name and user.first_name != tg_user.first_name:
        updates["first_name"] = tg_user.first_name
    if tg_user.last_name and user.last_name != tg_user.last_name:
        updates["last_name"] = tg_user.last_name
    if not created:
        if prefer_driver and user.role != UserRole.DRIVER.value:
            updates["role"] = UserRole.DRIVER.value
        if updates:
            User.update(**updates).where(User.id == user.id).execute()
            user = User.get_by_id(user.id)
    elif updates:
        User.update(**updates).where(User.id == user.id).execute()
        user = User.get_by_id(user.id)
    if user.role == UserRole.PASSENGER.value:
        PassengerProfile.get_or_create(user=user)
    if user.role == UserRole.DRIVER.value:
        DriverProfile.get_or_create(
            user=user,
            defaults={"status": DriverStatus.PENDING.value},
        )
    return user

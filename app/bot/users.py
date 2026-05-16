from aiogram.types import User as TgUser

from app.config import get_settings
from app.models import User, UserRole, PassengerProfile, DriverProfile, DriverStatus


def is_admin(telegram_id: int) -> bool:
    return telegram_id in get_settings().admin_ids


def ensure_user(tg_user: TgUser, *, prefer_driver: bool = False) -> User:
    tid = tg_user.id
    role = UserRole.DRIVER.value if prefer_driver else UserRole.PASSENGER.value
    user, created = User.get_or_create(
        telegram_id=tid,
        defaults={"username": tg_user.username, "role": role},
    )
    if not created:
        updates = {}
        if tg_user.username and user.username != tg_user.username:
            updates["username"] = tg_user.username
        if prefer_driver and user.role != UserRole.DRIVER.value:
            updates["role"] = UserRole.DRIVER.value
        if updates:
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

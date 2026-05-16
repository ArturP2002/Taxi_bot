import enum

from peewee import BigIntegerField, CharField, DateTimeField

from app.models.base import BaseModel
from app.util.datetimeutil import utcnow


class UserRole(str, enum.Enum):
    PASSENGER = "passenger"
    DRIVER = "driver"
    ADMIN = "admin"


class User(BaseModel):
    telegram_id = BigIntegerField(unique=True, index=True)
    role = CharField(max_length=32, default=UserRole.PASSENGER.value)
    username = CharField(max_length=255, null=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "users"

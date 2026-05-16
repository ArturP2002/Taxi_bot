from peewee import ForeignKeyField, DateTimeField

from app.models.base import BaseModel
from app.models.user import User
from app.util.datetimeutil import utcnow


class PassengerProfile(BaseModel):
    user = ForeignKeyField(User, unique=True, backref="passenger_profile", on_delete="CASCADE")
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "passenger_profiles"

import enum
from decimal import Decimal

from peewee import ForeignKeyField, IntegerField, CharField, BooleanField, TextField, DecimalField, DateTimeField

from app.models.base import BaseModel
from app.models.user import User
from app.models.direction import Direction
from app.util.datetimeutil import utcnow


class DriverStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPICIOUS = "suspicious"
    BLOCKED = "blocked"


class DriverProfile(BaseModel):
    user = ForeignKeyField(User, unique=True, backref="driver_profile", on_delete="CASCADE")
    direction = ForeignKeyField(Direction, null=True, backref="drivers", on_delete="SET NULL")
    max_seats = IntegerField(default=6)
    balance = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    status = CharField(max_length=32, default=DriverStatus.PENDING.value)
    online = BooleanField(default=False)
    current_city = TextField(null=True)
    full_name = TextField(null=True)
    car_info = TextField(null=True)
    phone = TextField(null=True)
    is_primary_on_direction = BooleanField(default=False)
    own_seats_reserved = IntegerField(default=0)
    loading = BooleanField(default=False)
    tariff_note = TextField(null=True)
    proposed_price_per_seat = DecimalField(max_digits=12, decimal_places=2, null=True)
    proposed_fixed_price = DecimalField(max_digits=12, decimal_places=2, null=True)
    pending_return_direction = ForeignKeyField(
        Direction, null=True, backref="drivers_pending_return", on_delete="SET NULL"
    )
    rest_until = DateTimeField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(null=True)

    class Meta:
        table_name = "driver_profiles"

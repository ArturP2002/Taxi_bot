import enum

from peewee import CharField, DateTimeField, ForeignKeyField, IntegerField, TextField

from app.models.base import BaseModel
from app.models.direction import Direction
from app.models.driver import DriverProfile
from app.util.datetimeutil import utcnow


class ScheduledTripStatus(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    FULL = "full"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ScheduledTripCreatedBy(str, enum.Enum):
    ADMIN = "admin"
    DRIVER = "driver"


class ScheduledTrip(BaseModel):
    direction = ForeignKeyField(Direction, backref="scheduled_trips", on_delete="CASCADE")
    departure_at = DateTimeField()
    seats_total = IntegerField()
    seats_booked = IntegerField(default=0)
    status = CharField(max_length=32, default=ScheduledTripStatus.OPEN.value)
    driver = ForeignKeyField(
        DriverProfile, null=True, backref="scheduled_trips", on_delete="SET NULL"
    )
    created_by = CharField(max_length=16, default=ScheduledTripCreatedBy.ADMIN.value)
    note = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(null=True)

    class Meta:
        table_name = "scheduled_trips"

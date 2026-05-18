import enum

from peewee import ForeignKeyField, CharField, DateTimeField

from app.models.base import BaseModel
from app.models.driver import DriverProfile
from app.models.order import Order
from app.util.datetimeutil import utcnow


class DriverEventType(str, enum.Enum):
    DECLINE = "decline"
    ORDER_CANCELLED = "order_cancelled"
    TRIP_COMPLETED = "trip_completed"


class DriverEvent(BaseModel):
    driver = ForeignKeyField(DriverProfile, backref="events", on_delete="CASCADE")
    order = ForeignKeyField(Order, null=True, backref="driver_events", on_delete="SET NULL")
    event_type = CharField(max_length=32)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "driver_events"
        indexes = ((("driver", "event_type", "created_at"), False),)

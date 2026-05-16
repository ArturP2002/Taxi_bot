from peewee import ForeignKeyField, IntegerField, DateTimeField

from app.models.base import BaseModel
from app.models.direction import Direction
from app.models.driver import DriverProfile
from app.util.datetimeutil import utcnow


class QueueEntry(BaseModel):
    direction = ForeignKeyField(Direction, backref="queue_entries", on_delete="CASCADE")
    driver = ForeignKeyField(DriverProfile, backref="queue_entries", on_delete="CASCADE")
    position = IntegerField(index=True)
    enqueued_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "queue_entries"
        indexes = ((("direction", "driver"), True),)

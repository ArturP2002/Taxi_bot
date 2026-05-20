from peewee import CharField, ForeignKeyField, IntegerField, DateTimeField

from app.models.base import BaseModel
from app.models.direction import Direction
from app.models.driver import DriverProfile
from app.util.datetimeutil import utcnow


class QueueEntry(BaseModel):
    direction = ForeignKeyField(Direction, backref="queue_entries", on_delete="CASCADE")
    driver = ForeignKeyField(DriverProfile, backref="queue_entries", on_delete="CASCADE")
    position = IntegerField(index=True)
    enqueued_at = DateTimeField(default=utcnow)
    loading_reminder_sent_at = DateTimeField(null=True)
    last_loading_notify_hash = CharField(max_length=64, null=True)

    class Meta:
        table_name = "queue_entries"
        indexes = ((("direction", "driver"), True),)

import enum

from peewee import CharField, DateTimeField, ForeignKeyField, IntegerField, TextField

from app.models.base import BaseModel
from app.models.direction import Direction
from app.util.datetimeutil import utcnow


class ReserveGroupStatus(str, enum.Enum):
    COLLECTING = "collecting"
    ACTIVATED = "activated"


class RouteReserveGroup(BaseModel):
    route_key = CharField(max_length=128, index=True)
    from_label = TextField()
    to_label = TextField()
    status = CharField(max_length=32, default=ReserveGroupStatus.COLLECTING.value)
    activated_direction = ForeignKeyField(
        Direction, null=True, backref="from_reserve_group", on_delete="SET NULL"
    )
    created_at = DateTimeField(default=utcnow)
    activated_at = DateTimeField(null=True)

    class Meta:
        table_name = "route_reserve_groups"

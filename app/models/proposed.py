import enum

from decimal import Decimal

from peewee import ForeignKeyField, TextField, IntegerField, CharField, DateTimeField, DecimalField

from app.models.base import BaseModel
from app.models.driver import DriverProfile
from app.models.direction import Direction
from app.models.reserve import RouteReserveGroup
from app.util.datetimeutil import utcnow


class ProposedStatus(str, enum.Enum):
    PENDING = "pending"
    RESERVED = "reserved"
    APPROVED = "approved"
    REJECTED = "rejected"


class ProposedDirection(BaseModel):
    proposer = ForeignKeyField(DriverProfile, backref="proposed_directions", on_delete="CASCADE")
    from_label = TextField()
    to_label = TextField()
    estimated_time_min = IntegerField()
    max_seats = IntegerField(default=6)
    own_seats = IntegerField(default=0)
    price_per_seat = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    fixed_price = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    comment = TextField(null=True)
    status = CharField(max_length=32, default=ProposedStatus.PENDING.value)
    reserve_group = ForeignKeyField(
        RouteReserveGroup,
        null=True,
        backref="proposals",
        on_delete="SET NULL",
    )
    created_direction = ForeignKeyField(Direction, null=True, backref="from_proposal", on_delete="SET NULL")
    admin_note = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    resolved_at = DateTimeField(null=True)

    class Meta:
        table_name = "proposed_directions"


class DirectionPioneer(BaseModel):
    """Drivers marked as primary pioneers for a direction (e.g. after route approval)."""

    direction = ForeignKeyField(Direction, backref="pioneers", on_delete="CASCADE")
    driver = ForeignKeyField(DriverProfile, backref="pioneer_directions", on_delete="CASCADE")

    class Meta:
        table_name = "direction_pioneers"
        indexes = ((("direction", "driver"), True),)

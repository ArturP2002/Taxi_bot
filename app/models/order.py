import enum

from peewee import (
    ForeignKeyField,
    TextField,
    IntegerField,
    CharField,
    DateTimeField,
)

from app.models.base import BaseModel
from app.models.direction import Direction
from app.models.driver import DriverProfile
from app.models.user import User
from app.util.datetimeutil import utcnow


class OrderStatus(str, enum.Enum):
    NEW = "new"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ADMIN_REVIEW = "admin_review"


class AssignmentStatus(str, enum.Enum):
    SUGGESTED = "suggested"
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class Order(BaseModel):
    direction = ForeignKeyField(Direction, backref="orders", on_delete="CASCADE")
    passenger = ForeignKeyField(User, backref="orders_as_passenger", on_delete="CASCADE")
    from_location = TextField()
    to_location = TextField()
    seats = IntegerField()
    phone = TextField()
    status = CharField(max_length=32, default=OrderStatus.NEW.value)
    confirmation_code_hash = TextField()
    code_issued_at = DateTimeField(null=True)
    code_consumed_at = DateTimeField(null=True)
    started_at = DateTimeField(null=True)
    ended_at = DateTimeField(null=True)
    pickup_location = TextField(null=True)
    pickup_time_text = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(null=True)

    class Meta:
        table_name = "orders"


class OrderDriverAssignment(BaseModel):
    order = ForeignKeyField(Order, backref="assignments", on_delete="CASCADE")
    driver = ForeignKeyField(DriverProfile, backref="assignments", on_delete="CASCADE")
    status = CharField(max_length=32, default=AssignmentStatus.PENDING.value)
    assigned_at = DateTimeField(default=utcnow)
    responded_at = DateTimeField(null=True)

    class Meta:
        table_name = "order_driver_assignments"

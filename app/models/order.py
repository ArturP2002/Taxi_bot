import enum

from decimal import Decimal

from peewee import (
    ForeignKeyField,
    TextField,
    IntegerField,
    CharField,
    BooleanField,
    DateTimeField,
    DecimalField,
)

from app.models.base import BaseModel
from app.models.direction import Direction
from app.models.driver import DriverProfile
from app.models.scheduled_trip import ScheduledTrip
from app.models.user import User
from app.util.datetimeutil import utcnow


class PassengerPaymentStatus(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    AWAITING = "awaiting"
    PAID = "paid"
    FAILED = "failed"


class OrderStatus(str, enum.Enum):
    NEW = "new"
    AWAITING_SCHEDULED_TRIP = "awaiting_scheduled_trip"
    AWAITING_PAYMENT = "awaiting_payment"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
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
    platform_seats = IntegerField(null=True)
    phone = TextField()
    status = CharField(max_length=32, default=OrderStatus.NEW.value)
    passenger_payment_status = CharField(
        max_length=32, default=PassengerPaymentStatus.NOT_REQUIRED.value
    )
    confirmation_code_hash = TextField()
    boarding_code = CharField(max_length=6, null=True)
    code_issued_at = DateTimeField(null=True)
    code_consumed_at = DateTimeField(null=True)
    started_at = DateTimeField(null=True)
    ended_at = DateTimeField(null=True)
    pickup_location = TextField(null=True)
    pickup_time_text = TextField(null=True)
    pickup_surcharge = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    transfer_requested_at = DateTimeField(null=True)
    transfer_note = TextField(null=True)
    scheduled_trip = ForeignKeyField(
        ScheduledTrip, null=True, backref="orders", on_delete="SET NULL"
    )
    scheduled_activated = BooleanField(default=False)
    requested_departure_at = DateTimeField(null=True)
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

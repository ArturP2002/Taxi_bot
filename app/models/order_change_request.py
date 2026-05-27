import enum

from peewee import CharField, DateTimeField, ForeignKeyField, TextField

from app.models.base import BaseModel
from app.models.order import Order
from app.models.user import User
from app.util.datetimeutil import utcnow


class OrderChangeRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class OrderChangeRequest(BaseModel):
    order = ForeignKeyField(Order, backref="change_requests", on_delete="CASCADE")
    passenger = ForeignKeyField(User, backref="order_change_requests", on_delete="CASCADE")
    status = CharField(max_length=32, default=OrderChangeRequestStatus.PENDING.value)
    requested_payload = TextField()
    admin_comment = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(null=True)

    class Meta:
        table_name = "order_change_requests"

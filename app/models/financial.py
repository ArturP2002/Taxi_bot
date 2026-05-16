import enum
from decimal import Decimal

from peewee import ForeignKeyField, DecimalField, CharField, TextField, DateTimeField

from app.models.base import BaseModel
from app.models.order import Order
from app.models.driver import DriverProfile
from app.util.datetimeutil import utcnow


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    AWAITING_ADMIN = "awaiting_admin"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class CommissionLedger(BaseModel):
    order = ForeignKeyField(Order, backref="commissions", on_delete="CASCADE")
    driver = ForeignKeyField(DriverProfile, backref="commissions", on_delete="CASCADE")
    amount = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    base_fare = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "commission_ledger"


class PaymentRecord(BaseModel):
    driver = ForeignKeyField(DriverProfile, backref="payments", on_delete="CASCADE")
    amount = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    status = CharField(max_length=32, default=PaymentStatus.PENDING.value)
    provider = CharField(max_length=64, null=True)
    provider_ref = TextField(null=True)
    raw_payload = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    confirmed_at = DateTimeField(null=True)

    class Meta:
        table_name = "payment_records"

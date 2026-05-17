from decimal import Decimal

from peewee import TextField, IntegerField, BooleanField, DecimalField, ForeignKeyField

from app.models.base import BaseModel


class Direction(BaseModel):
    from_label = TextField()
    to_label = TextField()
    estimated_time_min = IntegerField()
    min_time_percent = IntegerField(default=70)
    enabled = BooleanField(default=True)
    price_per_seat = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    fixed_price = DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    vehicle_capacity_default = IntegerField(default=6)
    online_payment_required = BooleanField(default=False)
    reverse_direction = ForeignKeyField("self", null=True, backref="reverse_of")

    class Meta:
        table_name = "directions"

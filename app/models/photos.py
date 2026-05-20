from peewee import CharField, DateTimeField, ForeignKeyField, IntegerField, TextField

from app.models.base import BaseModel
from app.models.driver import DriverProfile
from app.util.datetimeutil import utcnow


class DriverRegistrationPhoto(BaseModel):
    driver = ForeignKeyField(DriverProfile, backref="registration_photos", on_delete="CASCADE")
    kind = CharField(max_length=32)
    file_id = TextField()
    sort_order = IntegerField(default=0)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "driver_registration_photos"
        indexes = ((("driver", "kind"), False),)


class LoadingPhoto(BaseModel):
    driver = ForeignKeyField(DriverProfile, backref="loading_photos", on_delete="CASCADE")
    direction_id = IntegerField()
    session_id = CharField(max_length=64)
    file_id = TextField()
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "loading_photos"

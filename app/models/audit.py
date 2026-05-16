from peewee import BigIntegerField, TextField, DateTimeField

from app.models.base import BaseModel
from app.util.datetimeutil import utcnow


class AuditLog(BaseModel):
    actor_telegram_id = BigIntegerField(null=True)
    action = TextField()
    entity_type = TextField(null=True)
    entity_id = TextField(null=True)
    payload = TextField(null=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "audit_logs"

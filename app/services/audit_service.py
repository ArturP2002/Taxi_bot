import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.models import AuditLog


def log_action(
    action: str,
    *,
    actor_telegram_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    payload: Optional[Any] = None,
) -> None:
    AuditLog.create(
        actor_telegram_id=actor_telegram_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None,
        created_at=datetime.now(timezone.utc),
    )

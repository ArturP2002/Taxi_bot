import hashlib
import hmac
import json
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl

from app.config import get_settings


def validate_init_data(init_data: str, *, max_age_seconds: Optional[int] = 86400) -> Optional[Dict[str, Any]]:
    """
    Validate Telegram Mini App initData per
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        return None
    bot_token = (get_settings().bot_token or "").strip()
    if not bot_token:
        return None
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    hash_received = parsed.pop("hash", None)
    if not hash_received:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.HMAC(
        b"WebAppData",
        bot_token.encode(),
        hashlib.sha256,
    ).digest()
    computed = hmac.HMAC(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, hash_received):
        return None
    if max_age_seconds and "auth_date" in parsed:
        try:
            from time import time

            auth_date = int(parsed["auth_date"])
            if time() - auth_date > max_age_seconds:
                return None
        except (ValueError, TypeError):
            return None
    user_raw = parsed.get("user")
    user = None
    if user_raw:
        try:
            user = json.loads(user_raw)
        except json.JSONDecodeError:
            return None
    return {"user": user, "raw": parsed}

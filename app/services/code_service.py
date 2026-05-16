import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.config import get_settings


def generate_six_digit_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(order_id: int, code: str) -> str:
    pepper = get_settings().code_pepper.encode()
    msg = f"{order_id}:{code}".encode()
    return hmac.HMAC(pepper, msg, hashlib.sha256).hexdigest()


def verify_code(order_id: int, code: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_code(order_id, code), stored_hash)


def build_qr_token(order_id: int) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "oid": order_id,
        "jti": secrets.token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.qr_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def verify_qr_token(token: str) -> Optional[int]:
    settings = get_settings()
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return int(data["oid"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None

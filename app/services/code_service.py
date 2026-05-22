"""Boarding codes and QR payloads for passenger ↔ driver verification."""
from __future__ import annotations

import hashlib
import hmac
import io
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import jwt
import qrcode
from qrcode.constants import ERROR_CORRECT_M

from app.config import get_settings

logger = logging.getLogger("taxi_bot.code_service")

# Telegram /start payload: vc_{order_id}_{6-digit code}
_START_RE = re.compile(r"^vc_?(\d+)_(\d{6})$", re.IGNORECASE)
_COMPACT_RE = re.compile(r"^DBR[:|](\d+)[:|](\d{6})$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedVerification:
    order_id: int
    code: Optional[str]  # None for legacy JWT-only scan
    source: str  # code, compact, deeplink, jwt


def generate_six_digit_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(order_id: int, code: str) -> str:
    pepper = get_settings().code_pepper.encode()
    msg = f"{order_id}:{code}".encode()
    return hmac.new(pepper, msg, hashlib.sha256).hexdigest()


def verify_code(order_id: int, code: str, stored_hash: str) -> bool:
    code = normalize_boarding_code(code)
    if not code:
        return False
    return hmac.compare_digest(hash_code(order_id, code), stored_hash)


def normalize_boarding_code(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", (raw or "").strip())
    if len(digits) == 6:
        return digits
    if len(digits) == 0:
        return None
    if len(digits) < 6:
        return digits.zfill(6)
    if len(digits) > 6:
        return digits[-6:]
    return None


def build_start_param(order_id: int, code: str) -> str:
    code = normalize_boarding_code(code) or code
    return f"vc_{order_id}_{code}"


def build_compact_payload(order_id: int, code: str) -> str:
    code = normalize_boarding_code(code) or code
    return f"DBR:{order_id}:{code}"


def build_telegram_deeplink(bot_username: str, order_id: int, code: str) -> str:
    username = (bot_username or "").strip().lstrip("@")
    param = build_start_param(order_id, code)
    if username:
        return f"https://t.me/{username}?start={param}"
    return build_compact_payload(order_id, code)


def build_qr_token(order_id: int) -> str:
    """Legacy JWT QR (still accepted when scanned)."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "oid": order_id,
        "jti": secrets.token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.qr_token_ttl_minutes)).timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    if isinstance(token, bytes):
        return token.decode("ascii")
    return str(token)


def verify_qr_token(token: str) -> Optional[int]:
    settings = get_settings()
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return int(data["oid"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


def _extract_start_param(raw: str) -> str:
    text = (raw or "").strip()
    if "start=" in text:
        text = text.split("start=", 1)[-1].split("&")[0].split("#")[0]
    if "t.me/" in text and "/" in text:
        parts = text.rstrip("/").split("/")
        if parts:
            last = parts[-1]
            if last.startswith("vc"):
                return last
    return text


def parse_verification_raw(
    raw: str,
    *,
    default_order_id: Optional[int] = None,
) -> Optional[ParsedVerification]:
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("eyJ"):
        oid = verify_qr_token(text)
        if oid is not None:
            return ParsedVerification(order_id=oid, code=None, source="jwt")
        return None

    text = _extract_start_param(text)

    m = _START_RE.match(text)
    if m:
        return ParsedVerification(
            order_id=int(m.group(1)),
            code=m.group(2),
            source="deeplink",
        )

    m = _COMPACT_RE.match(text)
    if m:
        return ParsedVerification(
            order_id=int(m.group(1)),
            code=m.group(2),
            source="compact",
        )

    code = normalize_boarding_code(text)
    if code and default_order_id is not None:
        return ParsedVerification(
            order_id=default_order_id,
            code=code,
            source="code",
        )

    return None


def render_qr_png(payload: str) -> bytes:
    """High-contrast QR suitable for phone cameras."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def decode_qr_from_image_bytes(image_bytes: bytes) -> List[str]:
    """Decode QR payloads from a photo; empty list if decoder unavailable."""
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode as pyzbar_decode
    except ImportError:
        logger.warning("pyzbar/PIL not available — QR photo scan disabled")
        return []

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out: List[str] = []
        for item in pyzbar_decode(img):
            try:
                out.append(item.data.decode("utf-8").strip())
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("QR decode failed: %s", e)
        return []


def persist_boarding_code(order_id: int, code: str) -> None:
    from app.models import Order

    code = normalize_boarding_code(code) or code
    Order.update(
        boarding_code=code,
        confirmation_code_hash=hash_code(order_id, code),
    ).where(Order.id == order_id).execute()


def verification_error_label(key: str) -> str:
    labels = {
        "already_used": "Код уже использован.",
        "already_boarded": "Этот пассажир уже отмечен как посаженный.",
        "already_departed": "Поездка уже начата — посадка по этому заказу закрыта.",
        "no_active_assignment": "Нет принятого заказа у водителя.",
        "no_boarded_passengers": "Сначала отметьте посадку хотя бы одного пассажира (код/QR).",
        "trip_already_started": "Рейс уже в пути.",
        "bad_status": "Заказ не в статусе «назначен».",
        "invalid_token": "QR устарел или не для этого заказа.",
        "invalid_code": "Неверный код. Проверьте 6 цифр.",
        "wrong_order": "Код от другого заказа.",
        "invalid_format": "Не удалось прочитать код/QR.",
        "not_driver": "Подтверждение доступно только водителю.",
        "not_your_order": "Это не ваш заказ.",
        "code_not_found": "Код не найден среди ваших пассажиров на посадку.",
        "boarded": "ok",
    }
    return labels.get(key, key)

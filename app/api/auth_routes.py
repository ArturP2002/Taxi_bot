import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import get_settings
from app.models import User, UserRole
from app.rate_limit import limiter
from app.services.telegram_webapp import validate_init_data

logger = logging.getLogger("taxi_bot.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TelegramAuthIn(BaseModel):
    init_data: str


class TelegramAuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/telegram", response_model=TelegramAuthOut)
@limiter.limit("30/minute")
async def auth_telegram(request: Request, body: TelegramAuthIn) -> TelegramAuthOut:
    from app.db import ensure_connection

    try:
        ensure_connection()
        settings = get_settings()
        if not (settings.bot_token or "").strip():
            logger.error("BOT_TOKEN is empty")
            raise HTTPException(status_code=503, detail="bot_token_missing")
        parsed = validate_init_data(body.init_data)
        if not parsed or not parsed.get("user"):
            logger.warning(
                "bad_init_data (hash/date/user). bot_token set=%s init_len=%s",
                bool(settings.bot_token),
                len(body.init_data or ""),
            )
            raise HTTPException(status_code=401, detail="bad_init_data")
        tg_user = parsed["user"]
        tid = int(tg_user["id"])
        if not settings.admin_ids:
            logger.error("ADMIN_TELEGRAM_IDS is empty")
            raise HTTPException(status_code=503, detail="admin_not_configured")
        if tid not in settings.admin_ids:
            raise HTTPException(status_code=403, detail="not_admin")
        username = tg_user.get("username")
        user, created = User.get_or_create(
            telegram_id=tid,
            defaults={"username": username, "role": UserRole.ADMIN.value},
        )
        if not created:
            updates: dict = {}
            if username and user.username != username:
                updates["username"] = username
            if user.role != UserRole.ADMIN.value:
                updates["role"] = UserRole.ADMIN.value
            if updates:
                User.update(**updates).where(User.id == user.id).execute()
                user = User.get_by_id(user.id)
        exp = datetime.now(timezone.utc) + timedelta(days=7)
        token = jwt.encode(
            {"sub": str(tid), "exp": exp},
            settings.secret_key,
            algorithm="HS256",
        )
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return TelegramAuthOut(access_token=token)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("auth_telegram failed: %s", e)
        raise HTTPException(status_code=500, detail="auth_failed") from e

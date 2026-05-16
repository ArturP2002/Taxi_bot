from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import get_settings
from app.models import User
from app.rate_limit import limiter
from app.services.telegram_webapp import validate_init_data

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TelegramAuthIn(BaseModel):
    init_data: str


class TelegramAuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/telegram", response_model=TelegramAuthOut)
@limiter.limit("30/minute")
def auth_telegram(request: Request, body: TelegramAuthIn) -> TelegramAuthOut:
    parsed = validate_init_data(body.init_data)
    if not parsed or not parsed.get("user"):
        raise HTTPException(status_code=401, detail="bad_init_data")
    tg_user = parsed["user"]
    tid = int(tg_user["id"])
    settings = get_settings()
    if tid not in settings.admin_ids:
        raise HTTPException(status_code=403, detail="not_admin")
    user, created = User.get_or_create(
        telegram_id=tid,
        defaults={"username": tg_user.get("username")},
    )
    if not created and tg_user.get("username") and user.username != tg_user.get("username"):
        User.update(username=tg_user.get("username")).where(User.id == user.id).execute()
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    token = jwt.encode(
        {"sub": str(tid), "exp": exp},
        settings.secret_key,
        algorithm="HS256",
    )
    return TelegramAuthOut(access_token=token)

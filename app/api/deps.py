from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.models import User, UserRole

security = HTTPBearer(auto_error=False)


def decode_session(token: str) -> int:
    try:
        data = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
        return int(data["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="invalid_token")


async def get_current_user(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> User:
    if cred is None or cred.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="missing_token")
    tid = decode_session(cred.credentials)
    try:
        return User.get(telegram_id=tid)
    except User.DoesNotExist:
        settings = get_settings()
        if tid in settings.admin_ids:
            user, _ = User.get_or_create(
                telegram_id=tid,
                defaults={"role": UserRole.ADMIN.value},
            )
            return user
        raise HTTPException(status_code=401, detail="unknown_user")


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.telegram_id not in get_settings().admin_ids:
        raise HTTPException(status_code=403, detail="admin_only")
    return user

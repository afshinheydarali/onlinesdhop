import os
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash

from backend.db import SessionFactory
from backend.models import User
from backend.services.orders import Actor

passwords = PasswordHash.recommended()
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")
_secret = os.getenv("JWT_SECRET")
if not _secret:
    raise RuntimeError("JWT_SECRET is required")


def hash_password(value: str) -> str:
    return passwords.hash(value)


def verify_password(value: str, hashed: str) -> bool:
    return passwords.verify(value, hashed)


async def hash_password_async(value: str) -> str:
    return await anyio.to_thread.run_sync(hash_password, value)


async def verify_password_async(value: str, hashed: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password, value, hashed)


def make_token(user: User, minutes: int = 60) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.id),
            "exp": now + timedelta(minutes=minutes),
            "iat": now,
            "token_version": user.token_version,
        },
        _secret,
        algorithm="HS256",
    )


async def current_actor(token: str = Depends(oauth2)) -> Actor:
    try:
        claims = jwt.decode(
            token,
            _secret,
            algorithms=["HS256"],
            options={"require": ["exp", "sub", "token_version"]},
        )
        user_id = int(claims["sub"])
        token_version = claims["token_version"]
        if type(token_version) is not int or token_version < 0:
            raise ValueError("invalid token version")
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authentication"
        )
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active or user.token_version != token_version:
            raise HTTPException(status_code=401, detail="invalid authentication")
        return Actor(user_id=user.id, role=user.role, telegram_id=user.telegram_id)


def require(*roles: str) -> Any:
    async def dependency(actor: Actor = Depends(current_actor)) -> Actor:  # noqa: B008
        if actor.role not in roles:
            raise HTTPException(status_code=403, detail="forbidden")
        return actor

    return dependency

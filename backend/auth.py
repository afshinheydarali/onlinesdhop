import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

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

MAX_PASSWORD_BYTES = 128


def _password_within_limit(value: str) -> bool:
    return len(value.encode("utf-8")) <= MAX_PASSWORD_BYTES


@dataclass
class _FailureBucket:
    failures: int = 0
    window_started: float = 0.0
    blocked_until: float = 0.0
    last_seen: float = 0.0


class AuthRateLimiter:
    """Small process-local fail-closed limiter for password authentication."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: int = 60,
        max_failures: int = 5,
        block_seconds: int = 60,
        max_entries: int = 4096,
    ) -> None:
        self.clock = clock
        self.window_seconds = window_seconds
        self.max_failures = max_failures
        self.block_seconds = block_seconds
        self.max_entries = max_entries
        self._buckets: dict[tuple[str, str], _FailureBucket] = {}

    def _cleanup(self, now: float) -> None:
        expiry = now - max(self.window_seconds, self.block_seconds)
        self._buckets = {
            key: bucket
            for key, bucket in self._buckets.items()
            if bucket.last_seen >= expiry or bucket.blocked_until > now
        }

    def _keys(self, ip: str, username: str) -> tuple[tuple[str, str], ...]:
        return (("ip", ip), ("username", username), ("pair", f"{ip}\0{username}"))

    def check(self, ip: str, username: str) -> int | None:
        now = self.clock()
        self._cleanup(now)
        keys = self._keys(ip, username)
        retry_after = 0
        for key in keys:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_entries:
                    return self.block_seconds
                continue
            bucket.last_seen = now
            if bucket.blocked_until > now:
                retry_after = max(retry_after, int(bucket.blocked_until - now + 0.999))
        return retry_after or None

    def record_failure(self, ip: str, username: str) -> int | None:
        now = self.clock()
        self._cleanup(now)
        keys = self._keys(ip, username)
        if any(key not in self._buckets for key in keys) and len(self._buckets) + sum(key not in self._buckets for key in keys) > self.max_entries:
            return self.block_seconds
        for key in keys:
            bucket = self._buckets.setdefault(key, _FailureBucket(window_started=now, last_seen=now))
            if now - bucket.window_started >= self.window_seconds:
                bucket.failures = 0
                bucket.window_started = now
                bucket.blocked_until = 0.0
            bucket.failures += 1
            bucket.last_seen = now
            if bucket.failures >= self.max_failures:
                bucket.blocked_until = max(bucket.blocked_until, now + self.block_seconds)
        return self.check(ip, username)

    def record_success(self, ip: str, username: str) -> None:
        now = self.clock()
        for key in self._keys(ip, username):
            self._buckets.pop(key, None)
        self._cleanup(now)

    def reset(self) -> None:
        self._buckets.clear()


def hash_password(value: str) -> str:
    if not _password_within_limit(value):
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} UTF-8 bytes")
    return passwords.hash(value)


def verify_password(value: str, hashed: str) -> bool:
    if not _password_within_limit(value):
        return False
    return passwords.verify(value, hashed)


async def hash_password_async(value: str) -> str:
    return await anyio.to_thread.run_sync(hash_password, value)


async def verify_password_async(value: str, hashed: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password, value, hashed)


auth_rate_limiter = AuthRateLimiter()


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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authentication")
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

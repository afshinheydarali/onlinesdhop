from datetime import UTC, datetime, timedelta
import os
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from backend.db import SessionFactory
from backend.models import User
from backend.services.orders import Actor

passwords = PasswordHash.recommended()
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")
_secret = os.getenv("JWT_SECRET")
if not _secret:
    _secret = "local-development-only-change-me"

def hash_password(value: str) -> str: return passwords.hash(value)
def verify_password(value: str, hashed: str) -> bool: return passwords.verify(value, hashed)
def make_token(user: User, minutes: int = 60) -> str:
    now = datetime.now(UTC)
    return jwt.encode({"sub": str(user.id), "exp": now + timedelta(minutes=minutes), "iat": now, "token_version": user.token_version}, _secret, algorithm="HS256")

async def current_actor(token: str = Depends(oauth2)) -> Actor:
    try:
        claims = jwt.decode(token, _secret, algorithms=["HS256"])
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authentication")
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active or user.token_version != claims.get("token_version"):
            raise HTTPException(status_code=401, detail="invalid authentication")
        return Actor(user_id=user.id, role=user.role, telegram_id=user.telegram_id)

def require(*roles: str):
    async def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if actor.role not in roles: raise HTTPException(status_code=403, detail="forbidden")
        return actor
    return dependency

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost/onlineshop")


engine = create_async_engine(database_url(), pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


def pool_metrics() -> dict[str, int | str | None]:
    """Return non-secret pool pressure data where the configured pool exposes it."""
    pool = engine.pool
    status = getattr(pool, "status", None)
    if not callable(status):
        return {"supported": False}
    raw = status()
    # SQLAlchemy's status string is stable for the built-in queue pool, but keep
    # this parser deliberately tolerant for NullPool and future pool classes.
    values: dict[str, int | str | None] = {"supported": True, "raw": raw}
    import re

    labels = (("Pool size", "size"), ("Connections in pool", "idle"),
              ("Current Overflow", "overflow"),
              ("Current Checked out connections", "checked_out"))
    for label, key in labels:
        match = re.search(rf"{re.escape(label)}:\s*(-?\d+)", raw)
        if match:
            values[key] = int(match.group(1))
    return values


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session

"""Reset only the named local synthetic test database."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from scripts.db_guard import guarded_url


async def main() -> None:
    url = guarded_url()
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE TABLE outbox, idempotency_keys, stock_movements, reservations, "
            "order_items, orders, inventory_balances, products, admins, users "
            "RESTART IDENTITY CASCADE"
        ))
    await engine.dispose()
    print(f"reset {url.database}")


if __name__ == "__main__":
    asyncio.run(main())

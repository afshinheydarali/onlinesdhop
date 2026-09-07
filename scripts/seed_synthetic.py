"""Seed a small deterministic portfolio dataset into an explicitly allowed test DB."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from pwdlib import PasswordHash
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import Admin, InventoryBalance, Product, User
from scripts.db_guard import guarded_url

PASSWORD_HASH = PasswordHash.recommended().hash("portfolio-test-password")


async def reset(session) -> None:
    # Explicit table names make the destructive scope reviewable and require a
    # caller to pass --reset. PostgreSQL cascades only within this test DB.
    await session.execute(text(
        "TRUNCATE TABLE outbox, idempotency_keys, stock_movements, reservations, "
        "order_items, orders, inventory_balances, products, admins, users "
        "RESTART IDENTITY CASCADE"
    ))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="clear this disposable test DB before seeding")
    args = parser.parse_args()
    url = guarded_url()
    engine = create_async_engine(url, poolclass=NullPool)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as session:
        if args.reset:
            await reset(session)
        else:
            count = await session.scalar(text("SELECT count(*) FROM users"))
            if count:
                raise SystemExit("database is non-empty; pass --reset to replace synthetic data")
        owner = User(username="portfolio-owner", password_hash=PASSWORD_HASH, role="owner", is_active=True, token_version=0, telegram_id=700001)
        seller = User(username="portfolio-seller", password_hash=PASSWORD_HASH, role="seller", is_active=True, token_version=0, telegram_id=700002)
        session.add_all([
            owner,
            seller,
            Admin(telegram_id=700001, name="Synthetic Owner", admin_code="PORTFOLIO",
                  is_active=True, created_at=datetime(2026, 1, 1, tzinfo=UTC)),
            Admin(telegram_id=700002, name="Synthetic Seller", admin_code="SELLER",
                  is_active=True, created_at=datetime(2026, 1, 1, tzinfo=UTC)),
        ])
        await session.flush()
        for sku, name, price, stock in (("DEMO-RED", "Demo Red", 120_000, 5), ("DEMO-BLUE", "Demo Blue", 250_000, 2), ("DEMO-LAST", "Final Unit", 99_000, 1)):
            product = Product(sku=sku, name=name, unit_price=price, currency="IRR", is_active=True)
            session.add(product)
            await session.flush()
            session.add(InventoryBalance(product_id=product.id, on_hand=stock, reserved=0))
        await session.commit()
    await engine.dispose()
    print("seeded 2 users, 2 admins, 3 products")


if __name__ == "__main__":
    asyncio.run(main())

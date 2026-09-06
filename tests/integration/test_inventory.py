import asyncio
import os
import unittest

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

DB = os.getenv("TEST_DATABASE_URL")


def guarded(value):
    if not value:
        raise unittest.SkipTest("TEST_DATABASE_URL must be set")
    url = make_url(value)
    if url.host not in {"localhost", "127.0.0.1", "::1"} or not (url.database or "").endswith("_test"):
        raise RuntimeError("refusing non-local *_test database")
    return value


class InventoryPGTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(guarded(DB), poolclass=NullPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as c:
            await c.execute(
                text(
                    "TRUNCATE stock_movements, reservations, order_items, inventory_balances, "
                    "products, outbox, idempotency_keys, orders, admins, users "
                    "RESTART IDENTITY CASCADE"
                )
            )
        from backend.models import User

        async with self.sf() as s:
            u = User(username="inventory-owner", password_hash="x", role="owner", is_active=True, token_version=0)
            s.add(u)
            await s.flush()
            self.uid = u.id
            await s.commit()
        from backend.models import InventoryBalance, Product

        async with self.sf() as s:
            for sku in ("A", "B"):
                p = Product(sku=sku, name=sku, unit_price=100, currency="IRR", is_active=True)
                s.add(p)
                await s.flush()
                s.add(InventoryBalance(product_id=p.id, on_hand=5, reserved=0))
            await s.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def command(self, key, items):
        from backend.services.orders import CartLine, CreateCartOrderCommand

        return CreateCartOrderCommand("C", "09121234567", "T", "T", "A", None, tuple(CartLine(*x) for x in items), key)

    async def create(self, key, items):
        from backend.services.orders import Actor, OrderService

        async with self.sf() as s:
            return await OrderService(s).create_cart_order(self.command(key, items), Actor(self.uid, "owner"))

    async def test_two_buyers_final_unit_and_full_multiitem_rollback(self):
        from backend.models import InventoryBalance

        async with self.sf() as s:
            a = await s.scalar(select(InventoryBalance).where(InventoryBalance.product_id == 1))
            a.on_hand = 1
            await s.commit()
        barrier = asyncio.Barrier(2)

        async def run(key):
            await barrier.wait()
            try:
                return await self.create(key, (("A", 1),))
            except ValueError:
                return None

        results = await asyncio.wait_for(asyncio.gather(run("a"), run("b")), 10)
        self.assertEqual(sum(x is not None for x in results), 1)
        with self.assertRaises(ValueError):
            await self.create("short", (("A", 1), ("B", 6)))
        from backend.models import IdempotencyKey, InventoryBalance, Order, Outbox, Reservation, StockMovement

        async with self.sf() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(Reservation)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(Outbox)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(StockMovement)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(IdempotencyKey)), 1)
            a = await s.scalar(select(InventoryBalance).where(InventoryBalance.product_id == 1))
            self.assertEqual(a.reserved, 1)

    async def test_opposite_sku_order_no_deadlock_and_snapshot(self):
        await asyncio.wait_for(asyncio.gather(self.create("one", (("A", 1), ("B", 1))), self.create("two", (("B", 1), ("A", 1)))), 10)
        from backend.models import OrderItem, Product

        async with self.sf() as s:
            p = await s.scalar(select(Product).where(Product.sku == "A"))
            p.name = "Changed"
            p.unit_price = 999
            await s.commit()
            item = await s.scalar(select(OrderItem).where(OrderItem.sku_snapshot == "A"))
            self.assertEqual((item.name_snapshot, item.unit_price_snapshot), ("A", 100))

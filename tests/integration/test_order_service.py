import os
import unittest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

os.environ.setdefault("JWT_SECRET", "integration-test-secret-32-characters-long")
DB = os.getenv("TEST_DATABASE_URL")

class OrderServicePGTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not DB or "_test" not in DB: self.skipTest("TEST_DATABASE_URL must explicitly target *_test")
        self.engine = create_async_engine(DB, poolclass=NullPool)
        from backend.models import IdempotencyKey, Order, Outbox, User
        async with self.engine.begin() as c:
            await c.execute(delete(Outbox)); await c.execute(delete(IdempotencyKey)); await c.execute(delete(Order)); await c.execute(delete(User))
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        if hasattr(self, "engine"): await self.engine.dispose()

    async def test_api_actor_create_replay_and_atomic_outbox(self):
        from backend.models import User
        from backend.services.orders import Actor, CreateOrderCommand, OrderService
        async with self.sf() as s:
            user = User(username="pg-owner", password_hash="x", role="owner", is_active=True, token_version=0); s.add(user); await s.flush(); uid = user.id
            command = CreateOrderCommand("Customer", "09121234567", "989121234567", "Tehran", "Tehran", "Street", None, "Widget", "widget", 1, None, None, "", "idem-1")
            first = await OrderService(s).create_order(command, Actor(uid, "owner")); self.assertTrue(first.created); self.assertIsNotNone(first.order)
        async with self.sf() as s:
            second = await OrderService(s).create_order(command, Actor(uid, "owner")); self.assertFalse(second.created); self.assertEqual(second.order.id, first.order.id)
            self.assertEqual(await s.scalar(select(func.count()).select_from(__import__("backend.models", fromlist=["Outbox"]).Outbox)), 1)

if __name__ == "__main__": unittest.main()

import asyncio
import os
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("JWT_SECRET", "integration-test-secret-32-characters-long")
DB = os.getenv("TEST_DATABASE_URL")


def guarded_url(value):
    if not value:
        raise unittest.SkipTest("TEST_DATABASE_URL must be set")
    url = make_url(value)
    if not (url.database or "").endswith("_test") or url.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise RuntimeError("refusing non-local *_test database")
    return value


class OrderServicePGTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(guarded_url(DB), poolclass=NullPool)
        async with self.engine.begin() as c:
            await c.execute(
                text(
                    "TRUNCATE TABLE outbox, idempotency_keys, orders, admins, users RESTART IDENTITY CASCADE"
                )
            )
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.models import User

        async with self.sf() as s:
            users = [
                User(
                    username="pg-owner",
                    password_hash="x",
                    role="owner",
                    is_active=True,
                    token_version=0,
                ),
                User(
                    username="pg-manager",
                    password_hash="x",
                    role="manager",
                    is_active=True,
                    token_version=0,
                ),
            ]
            s.add_all(users)
            await s.flush()
            self.uids = [u.id for u in users]
            await s.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def command(self, key, phone="09121234567", product="Widget"):
        from backend.services.orders import CreateOrderCommand
        from order_bot.validation import normalize_phone, normalize_product

        return CreateOrderCommand(
            "Customer",
            phone,
            normalize_phone(phone),
            "Tehran",
            "Tehran",
            "Street",
            None,
            product,
            normalize_product(product),
            1,
            None,
            None,
            "",
            key,
        )

    async def create(self, uid, command, role="owner"):
        from backend.services.orders import Actor, OrderService

        async with self.sf() as s:
            return await OrderService(s).create_order(command, Actor(uid, role))

    async def test_api_actor_create_replay_and_atomic_outbox(self):
        first = await self.create(self.uids[0], self.command("replay"))
        second = await self.create(self.uids[0], self.command("replay"))
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.order.id, second.order.id)
        from backend.models import Outbox

        async with self.sf() as s:
            self.assertEqual(
                await s.scalar(select(func.count()).select_from(Outbox)), 1
            )

    async def test_concurrent_duplicate_warning_is_anonymous(self):
        barrier = asyncio.Barrier(2)

        async def run(key):
            await barrier.wait()
            return await self.create(self.uids[0], self.command(key))

        results = await asyncio.wait_for(asyncio.gather(run("d1"), run("d2")), 10)
        self.assertEqual(sum(r.created for r in results), 1)
        warning = next(r for r in results if r.duplicate_confirmation_required)
        self.assertIsNone(warning.order)

    async def test_same_actor_key_different_payload_conflicts(self):
        from backend.services.orders import IdempotencyConflict

        barrier = asyncio.Barrier(2)

        async def run(phone):
            await barrier.wait()
            return await self.create(self.uids[0], self.command("same", phone=phone))

        results = await asyncio.wait_for(
            asyncio.gather(
                run("09121234567"), run("09129876543"), return_exceptions=True
            ),
            10,
        )
        self.assertEqual(sum(isinstance(r, IdempotencyConflict) for r in results), 1)
        self.assertEqual(sum(getattr(r, "created", False) for r in results), 1)
        from backend.models import Order

        async with self.sf() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(Order)), 1)

    async def test_same_actor_key_same_payload_concurrent_replays(self):
        barrier = asyncio.Barrier(2)

        async def run():
            await barrier.wait()
            return await self.create(self.uids[0], self.command("same-payload"))

        results = await asyncio.wait_for(asyncio.gather(run(), run()), 10)
        self.assertEqual(sum(r.created for r in results), 1)
        self.assertEqual(results[0].order.id, results[1].order.id)

    async def test_same_key_different_actors_are_isolated(self):
        results = await asyncio.wait_for(
            asyncio.gather(
                self.create(self.uids[0], self.command("shared", product="A")),
                self.create(
                    self.uids[1], self.command("shared", product="B"), "manager"
                ),
            ),
            10,
        )
        self.assertEqual(sum(r.created for r in results), 2)

    async def test_outbox_failure_rolls_back_order_and_idempotency(self):
        from backend.models import IdempotencyKey, Order, Outbox
        from backend.services.orders import Actor, OrderService

        async with self.sf() as s:
            original_add = s.add

            def fail(value):
                if isinstance(value, Outbox):
                    raise TypeError("injected outbox failure")
                original_add(value)

            with patch.object(s, "add", side_effect=fail), self.assertRaises(TypeError):
                await OrderService(s).create_order(
                    self.command("rollback"), Actor(self.uids[0], "owner")
                )
            await s.rollback()
        async with self.sf() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(Order)), 0)
            self.assertEqual(
                await s.scalar(select(func.count()).select_from(IdempotencyKey)), 0
            )
            self.assertEqual(
                await s.scalar(select(func.count()).select_from(Outbox)), 0
            )

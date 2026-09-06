"""Deterministic PostgreSQL outbox worker acceptance tests (case 9)."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

DB = os.getenv("TEST_DATABASE_URL")


def guarded_url(value: str | None) -> str:
    if not value:
        raise unittest.SkipTest("TEST_DATABASE_URL must be set")
    parsed = make_url(value)
    if parsed.host not in {"localhost", "127.0.0.1", "::1"} or not (parsed.database or "").endswith("_test"):
        raise RuntimeError("refusing non-local *_test database")
    return value


class FakeTransport:
    def __init__(self, *, fail_text_once: bool = False, timeout: bool = False):
        self.fail_text_once = fail_text_once
        self.timeout = timeout
        self.photos = 0
        self.texts = 0

    async def send_photo(self, order) -> int:
        self.photos += 1
        return 500 + self.photos

    async def send_text(self, order, photo_message_id: int) -> int | None:
        self.texts += 1
        if self.timeout:
            raise TimeoutError("provider timeout")
        if self.fail_text_once and self.texts == 1:
            from backend.services.delivery import TransientDeliveryError

            raise TransientDeliveryError(retry_after=0)
        return 600 + self.texts


class OutboxPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(guarded_url(DB), poolclass=NullPool)
        async with self.engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE TABLE payment_events, payment_attempts, outbox, idempotency_keys, orders, admins, users RESTART IDENTITY CASCADE"
            ))
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.models import Order, Outbox

        async with self.sf() as session:
            order = Order(
                public_id="ORD-OUTBOX-1", customer_name="Customer", phone_raw="0912", phone_normalized="98912",
                province="Tehran", city="Tehran", address="Street", product_raw="Widget",
                product_normalized="widget", quantity=1, amount=1000, photo_file_id="photo", draft_token="outbox-draft",
                created_at=datetime.now(UTC), delivery_status="pending", delivery_attempts=0,
                payment_status="pending", payment_currency="IRR", fulfillment_status="confirmed",
            )
            session.add(order)
            await session.flush()
            session.add(Outbox(order_id=order.id, status="pending", attempts=0, next_attempt_at=datetime.now(UTC)))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_two_workers_have_one_claim_and_stale_token_is_fenced(self) -> None:
        from backend.services.delivery import DeliveryService

        barrier = asyncio.Barrier(2)

        async def claim(worker: str):
            await barrier.wait()
            async with self.sf() as session:
                return await DeliveryService(session).claim_next(worker_id=worker)

        claims = await asyncio.wait_for(asyncio.gather(claim("one"), claim("two")), 10)
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        valid = next(claim for claim in claims if claim is not None)
        async with self.sf() as session:
            from backend.services.orders import OrderService

            with self.assertRaises(ValueError):
                await OrderService(session).mark_delivery_sent(valid.order_id, "stale-token", 501, 601)
            await session.rollback()

    async def test_photo_is_persisted_before_retrying_text(self) -> None:
        from backend.models import Order, Outbox
        from backend.services.delivery import DeliveryWorker

        transport = FakeTransport(fail_text_once=True)
        worker = DeliveryWorker(self.sf, transport, worker_id="worker-partial")
        self.assertTrue(await worker.run_once())
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-OUTBOX-1"))
            outbox = await session.scalar(select(Outbox).where(Outbox.order_id == order.id))
            order_id = order.id
            self.assertEqual(order.channel_photo_message_id, 501)
            outbox.next_attempt_at = datetime.now(UTC)
            await session.commit()
        self.assertTrue(await worker.run_once())
        async with self.sf() as session:
            order = await session.get(Order, order_id)
            self.assertEqual(order.channel_photo_message_id, 501)
            self.assertEqual(order.channel_text_message_id, 602)
            self.assertEqual(order.delivery_status, "sent")
        self.assertEqual(transport.photos, 1)
        self.assertEqual(transport.texts, 2)

    async def test_timeout_becomes_ambiguous_until_explicit_reconciliation(self) -> None:
        from backend.models import Outbox
        from backend.services.delivery import DeliveryWorker
        from backend.services.orders import OrderService

        worker = DeliveryWorker(self.sf, FakeTransport(timeout=True), worker_id="worker-timeout")
        self.assertTrue(await worker.run_once())
        async with self.sf() as session:
            outbox = await session.scalar(select(Outbox))
            self.assertEqual(outbox.status, "ambiguous")
            await OrderService(session).reconcile_ambiguous_delivery(outbox.order_id)
            outbox = await session.scalar(select(Outbox))
            self.assertEqual(outbox.status, "pending")

"""Deterministic PostgreSQL outbox worker acceptance tests (case 9)."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta

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
                "TRUNCATE TABLE payment_events, payment_reconciliations, payment_attempts, "
                "fulfillment_transitions, outbox, idempotency_keys, orders, admins, users "
                "RESTART IDENTITY CASCADE"
            ))
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.models import Order, Outbox

        async with self.sf() as session:
            order = Order(
                public_id="ORD-OUTBOX-1", customer_name="Customer", phone_raw="0912", phone_normalized="98912",
                province="Tehran", city="Tehran", address="Street", product_raw="Widget",
                product_normalized="widget", quantity=1, amount=1000, photo_file_id="photo", draft_token="outbox-draft",
                created_at=datetime.now(UTC), delivery_status="pending", delivery_attempts=0,
                currency="IRR", payment_status="pending", fulfillment_status="confirmed",
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

    async def test_live_lease_is_unstealable_and_expired_uncertain_is_ambiguous(self) -> None:
        from backend.models import Order, Outbox
        from backend.services.delivery import DeliveryService

        async with self.sf() as session:
            first = await DeliveryService(session).claim_next(worker_id="live", lease_seconds=120)
            self.assertIsNotNone(first)
        async with self.sf() as session:
            self.assertIsNone(await DeliveryService(session).claim_next(worker_id="thief"))
        async with self.sf() as session:
            row = await session.scalar(select(Outbox))
            row.lease_expires_at = datetime.now(UTC)
            await session.commit()
        async with self.sf() as session:
            self.assertIsNone(await DeliveryService(session).claim_next(worker_id="recovery"))
        async with self.sf() as session:
            row = await session.scalar(select(Outbox))
            order = await session.scalar(select(Order))
            self.assertEqual((row.status, row.error_code, order.delivery_status), ("ambiguous", "lease_expired_manual_reconciliation", "ambiguous"))

    async def test_attempt_cap_and_rate_hint_are_bounded(self) -> None:
        from backend.models import Outbox
        from backend.services.delivery import DeliveryService

        async with self.sf() as session:
            row = await session.scalar(select(Outbox))
            row.status, row.attempts, row.next_attempt_at = "pending", 5, datetime.now(UTC)
            await session.commit()
        async with self.sf() as session:
            self.assertIsNone(await DeliveryService(session).claim_next(worker_id="capped"))
            row = await session.scalar(select(Outbox))
            self.assertEqual((row.status, row.error_code, row.next_attempt_at), ("failed", "attempt_limit", None))
        before = datetime.now(UTC)
        retry_at = DeliveryService._backoff(1, 10**9)
        self.assertGreaterEqual((retry_at - before).total_seconds(), 3600)
        self.assertLess((retry_at - before).total_seconds(), 3602)

    async def test_expired_claim_fences_every_finalizer_and_live_claim_succeeds(self) -> None:
        from backend.models import Order, Outbox
        from backend.services.delivery import DeliveryService
        from backend.services.orders import OrderService

        async def reset() -> None:
            async with self.sf() as session:
                row = await session.scalar(select(Outbox))
                order = await session.scalar(select(Order))
                row.status, row.worker_id, row.claim_token = "pending", None, None
                row.lease_expires_at, row.attempts = None, 0
                row.next_attempt_at, row.error_code = datetime.now(UTC), None
                order.delivery_status, order.delivery_error = "pending", None
                order.channel_photo_message_id, order.channel_text_message_id = None, None
                await session.commit()

        operations = [
            lambda service, claim: service.mark_photo_sent(claim.order_id, claim.claim_token, 701),
            lambda service, claim: service.mark_delivery_sent(claim.order_id, claim.claim_token, 701, 702),
            lambda service, claim: service.mark_delivery_failed(claim.order_id, claim.claim_token, "late-finalize"),
            lambda service, claim: service.mark_delivery_ambiguous(claim.order_id, claim.claim_token, "late-finalize"),
        ]
        for operation in operations:
            await reset()
            async with self.sf() as session:
                claim = await DeliveryService(session).claim_next(worker_id="stale", lease_seconds=300)
            self.assertIsNotNone(claim)
            async with self.sf() as session:
                row = await session.scalar(select(Outbox))
                row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            async with self.sf() as session:
                with self.assertRaises(ValueError):
                    await operation(OrderService(session), claim)
            async with self.sf() as session:
                row = await session.scalar(select(Outbox))
                order = await session.scalar(select(Order))
                self.assertEqual((row.status, row.error_code, order.delivery_status), ("ambiguous", "lease_expired_manual_reconciliation", "ambiguous"))

        await reset()
        async with self.sf() as session:
            claim = await DeliveryService(session).claim_next(worker_id="live", lease_seconds=300)
            self.assertIsNotNone(claim)
            service = OrderService(session)
            await service.mark_photo_sent(claim.order_id, claim.claim_token, 703)
            await service.mark_delivery_sent(claim.order_id, claim.claim_token, 703, 704)
        async with self.sf() as session:
            row = await session.scalar(select(Outbox))
            order = await session.scalar(select(Order))
            self.assertEqual((row.status, order.delivery_status, order.channel_text_message_id), ("sent", "sent", 704))

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
        from backend.services.delivery import DeliveryService, DeliveryWorker
        from backend.services.orders import OrderService

        worker = DeliveryWorker(self.sf, FakeTransport(timeout=True), worker_id="worker-timeout")
        self.assertTrue(await worker.run_once())
        async with self.sf() as session:
            outbox = await session.scalar(select(Outbox))
            self.assertEqual(outbox.status, "ambiguous")
            self.assertEqual(len(await DeliveryService(session).list_reconciliation()), 1)
            await OrderService(session).reconcile_ambiguous_delivery(outbox.order_id)
            outbox = await session.scalar(select(Outbox))
            self.assertEqual(outbox.status, "pending")

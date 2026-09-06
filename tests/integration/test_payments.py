"""Deterministic fake-payment PostgreSQL acceptance tests (case 7)."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from sqlalchemy import func, select, text
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


class PaymentPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(guarded_url(DB), poolclass=NullPool)
        async with self.engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE TABLE payment_events, payment_attempts, outbox, idempotency_keys, orders, admins, users RESTART IDENTITY CASCADE"
            ))
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.models import Order, Outbox, User

        async with self.sf() as session:
            user = User(username="payment-owner", password_hash="x", role="owner", is_active=True, token_version=0)
            session.add(user)
            await session.flush()
            order = Order(
                public_id="ORD-PAYMENT-1", created_by_id=user.id, customer_name="Customer",
                phone_raw="0912", phone_normalized="98912", province="Tehran", city="Tehran",
                address="Street", product_raw="Widget", product_normalized="widget", quantity=1,
                amount=1000, notes=None, photo_file_id="", draft_token="payment-draft",
                created_at=datetime.now(UTC), payment_currency="IRR", payment_status="pending",
                fulfillment_status="confirmed", delivery_status="pending", delivery_attempts=0,
            )
            session.add(order)
            await session.flush()
            session.add(Outbox(order_id=order.id, status="pending", attempts=0, next_attempt_at=datetime.now(UTC)))
            await session.commit()
        self.secret = "payment-test-secret"

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def raw(self, event: str = "event-1", tx: str = "transaction-1", amount: int = 1000) -> bytes:
        from backend.services.payments import FakeGateway

        return FakeGateway.payload(order_id="ORD-PAYMENT-1", amount=amount, provider_transaction_id=tx, provider_event_id=event)

    async def test_valid_replay_and_invalid_inputs_have_one_effect(self) -> None:
        from backend.models import Order, PaymentAttempt, PaymentEvent
        from backend.services.payments import FakeGateway, InvalidPaymentSignature, PaymentConflict, PaymentError, PaymentService

        raw = self.raw()
        async with self.sf() as session:
            first = await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))
        self.assertTrue(first.applied)
        async with self.sf() as session:
            duplicate = await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            self.assertEqual(duplicate.status, "duplicate")
            self.assertEqual(order.payment_status, "paid")
        async with self.sf() as session:
            with self.assertRaises(InvalidPaymentSignature):
                await PaymentService(session, secret=self.secret).apply_callback(raw, "sha256=" + "00" * 32)
            await session.rollback()
            bad_amount = self.raw(event="event-bad", amount=999)
            with self.assertRaises(PaymentError):
                await PaymentService(session, secret=self.secret).apply_callback(
                    bad_amount, FakeGateway.sign(bad_amount, self.secret)
                )
            await session.rollback()
            changed = raw.replace(b"transaction-1", b"transaction-2")
            with self.assertRaises(PaymentConflict):
                await PaymentService(session, secret=self.secret).apply_callback(changed, FakeGateway.sign(changed, self.secret))
            await session.rollback()
        async with self.sf() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(PaymentAttempt)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(PaymentEvent)), 1)

    async def test_second_event_same_transaction_is_deduplicated_concurrently(self) -> None:
        from backend.models import PaymentAttempt
        from backend.services.payments import FakeGateway, PaymentService

        barrier = asyncio.Barrier(2)

        async def receive(event: str):
            raw = self.raw(event=event, tx="same-transaction")
            await barrier.wait()
            async with self.sf() as session:
                try:
                    return await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))
                except Exception as exc:  # assertion below keeps the race result explicit
                    await session.rollback()
                    return exc

        results = await asyncio.wait_for(asyncio.gather(receive("event-a"), receive("event-b")), 10)
        self.assertEqual(sum(getattr(result, "applied", False) for result in results), 1)
        self.assertEqual(sum(getattr(result, "status", None) == "duplicate" for result in results), 1)
        async with self.sf() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(PaymentAttempt)), 1)

    async def test_http_callback_authenticates_exact_raw_bytes(self) -> None:
        from backend.api.app import app
        from backend.services.payments import FakeGateway

        raw = self.raw(event="http-event", tx="http-transaction")
        signature = FakeGateway.sign(raw, self.secret)
        with patch.dict(os.environ, {"FAKE_PAYMENT_HMAC_SECRET": self.secret}), patch("backend.api.app.SessionFactory", self.sf):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/payments/fake/callback",
                    content=raw + b" ",
                    headers={"X-Fake-Gateway-Signature": signature},
                )
                self.assertEqual(response.status_code, 401)
                response = await client.post(
                    "/api/v1/payments/fake/callback",
                    content=raw,
                    headers={"X-Fake-Gateway-Signature": signature},
                )
                self.assertEqual(response.status_code, 200)

    async def test_late_payment_is_reconciliation_and_order_stays_expired(self) -> None:
        from backend.models import Order
        from backend.services.payments import FakeGateway, PaymentService

        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            order.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        raw = self.raw(event="late-event", tx="late-transaction")
        async with self.sf() as session:
            result = await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            self.assertEqual(result.status, "reconciliation")
            self.assertEqual(order.payment_status, "reconciliation")
            self.assertEqual(order.fulfillment_status, "expired")

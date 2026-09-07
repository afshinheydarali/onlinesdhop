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
                "TRUNCATE TABLE payment_events, payment_reconciliations, payment_attempts, "
                "fulfillment_transitions, stock_movements, reservations, inventory_balances, products, "
                "outbox, idempotency_keys, orders, admins, users "
                "RESTART IDENTITY CASCADE"
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
                amount=1000, currency="IRR", notes=None, photo_file_id="", draft_token="payment-draft",
                created_at=datetime.now(UTC), payment_status="pending",
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

    async def add_reservation(self, *, expires_at: datetime | None = None) -> None:
        from backend.models import InventoryBalance, Order, Product, Reservation

        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            product = Product(sku="PAYMENT-WIDGET", name="Payment Widget", unit_price=1000, currency="IRR", is_active=True)
            session.add(product)
            await session.flush()
            session.add_all([
                InventoryBalance(product_id=product.id, on_hand=1, reserved=1),
                Reservation(order_id=order.id, product_id=product.id, quantity=1, status="reserved", expires_at=expires_at),
            ])
            await session.commit()

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

    async def test_invalid_binding_cases_have_zero_mutation(self) -> None:
        """Every rejected callback is checked against all payment tables."""
        from backend.models import Order, PaymentAttempt, PaymentEvent
        from backend.services.payments import FakeGateway, InvalidPaymentSignature, PaymentError, PaymentService

        async def counts(session):
            values = []
            for table in (Order, PaymentAttempt, PaymentEvent):
                values.append(await session.scalar(select(func.count()).select_from(table)))
            return tuple(values)

        async with self.sf() as session:
            before = await counts(session)

        cases = [
            ("bad-signature", self.raw(event="bad-signature"), InvalidPaymentSignature, "sha256=" + "00" * 32),
            ("wrong-amount", self.raw(event="wrong-amount", amount=999), PaymentError, None),
            (
                "wrong-currency",
                FakeGateway.payload(
                    order_id="ORD-PAYMENT-1", amount=1000, currency="USD",
                    provider_transaction_id="wrong-currency", provider_event_id="wrong-currency",
                ),
                PaymentError,
                None,
            ),
            (
                "unknown-order",
                FakeGateway.payload(
                    order_id="ORD-MISSING", amount=1000,
                    provider_transaction_id="unknown-order", provider_event_id="unknown-order",
                ),
                PaymentError,
                None,
            ),
        ]
        for _, raw, error, signature in cases:
            async with self.sf() as session:
                with self.assertRaises(error):
                    await PaymentService(session, secret=self.secret).apply_callback(
                        raw, signature or FakeGateway.sign(raw, self.secret)
                    )
                await session.rollback()
            async with self.sf() as session:
                self.assertEqual(await counts(session), before)

    async def test_changed_event_payload_and_transaction_binding_are_fenced(self) -> None:
        from backend.models import PaymentAttempt, PaymentEvent
        from backend.services.payments import FakeGateway, PaymentConflict, PaymentService

        raw = self.raw(event="binding-event", tx="binding-tx")
        async with self.sf() as session:
            await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))
        changed_event = FakeGateway.payload(
            order_id="ORD-PAYMENT-1", amount=1000, provider_transaction_id="binding-tx",
            provider_event_id="binding-event", status="failed",
        )
        async with self.sf() as session:
            with self.assertRaises(PaymentConflict):
                await PaymentService(session, secret=self.secret).apply_callback(
                    changed_event, FakeGateway.sign(changed_event, self.secret)
                )
            await session.rollback()
        # A transaction already bound to this order can be delivered by a
        # second event, but it must not create another attempt or apply twice.
        second_event = self.raw(event="binding-event-2", tx="binding-tx")
        async with self.sf() as session:
            result = await PaymentService(session, secret=self.secret).apply_callback(
                second_event, FakeGateway.sign(second_event, self.secret)
            )
            self.assertEqual(result.status, "duplicate")
            self.assertFalse(result.applied)
            self.assertEqual(await session.scalar(select(func.count()).select_from(PaymentAttempt)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(PaymentEvent)), 2)

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
        from backend.models import InventoryBalance, Order, PaymentReconciliation, Reservation, StockMovement
        from backend.services.payments import FakeGateway, PaymentService

        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            await session.commit()
        await self.add_reservation(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        raw = self.raw(event="late-event", tx="late-transaction")
        async with self.sf() as session:
            result = await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            self.assertEqual(result.status, "reconciliation")
            self.assertEqual(order.payment_status, "paid")
            self.assertTrue(order.reconciliation_required)
            self.assertEqual(order.fulfillment_status, "expired")
            balance = await session.scalar(select(InventoryBalance))
            reservation = await session.scalar(select(Reservation))
            self.assertEqual(balance.reserved, 0)
            self.assertEqual(reservation.status, "expired")
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(StockMovement).where(StockMovement.movement_type == "release")),
                1,
            )
            reconciliation = await session.scalar(select(PaymentReconciliation).where(PaymentReconciliation.order_id == order.id))
            self.assertEqual((reconciliation.kind, reconciliation.status), ("late_payment", "open"))

    async def test_payment_vs_expiry_order_lock_makes_late_money_reconciliation(self) -> None:
        from backend.models import InventoryBalance, Order, Reservation
        from backend.services.payments import FakeGateway, PaymentService

        expiry_has_lock = asyncio.Event()
        release_expiry = asyncio.Event()
        payment_started = asyncio.Event()
        await self.add_reservation(expires_at=datetime.now(UTC) - timedelta(seconds=1))

        async def expire_first() -> None:
            async with self.sf() as session:
                order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1").with_for_update())
                expiry_has_lock.set()
                await release_expiry.wait()
                order.fulfillment_status = "expired"
                await session.commit()

        async def payment_after_expiry_lock():
            await expiry_has_lock.wait()
            payment_started.set()
            raw = self.raw(event="race-expiry", tx="race-expiry-tx")
            async with self.sf() as session:
                return await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))

        expiry_task = asyncio.create_task(expire_first())
        await expiry_has_lock.wait()
        payment_task = asyncio.create_task(payment_after_expiry_lock())
        await payment_started.wait()
        release_expiry.set()
        result = await asyncio.wait_for(payment_task, 10)
        await asyncio.wait_for(expiry_task, 10)
        self.assertEqual(result.status, "reconciliation")
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            balance = await session.scalar(select(InventoryBalance))
            reservation = await session.scalar(select(Reservation))
            self.assertEqual(order.fulfillment_status, "expired")
            self.assertEqual((balance.reserved, reservation.status), (0, "expired"))

    async def test_payment_vs_cancel_order_lock_never_revives_or_rereserves(self) -> None:
        from backend.models import InventoryBalance, Order, StockMovement
        from backend.services.payments import FakeGateway, PaymentService

        cancel_has_lock = asyncio.Event()
        release_cancel = asyncio.Event()
        payment_started = asyncio.Event()
        await self.add_reservation()

        async def cancel_first() -> None:
            async with self.sf() as session:
                order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1").with_for_update())
                from backend.services.fulfillment import FulfillmentService

                service = FulfillmentService(session)
                cancel_has_lock.set()
                await release_cancel.wait()
                await service._release_locked(order, status="released", at=datetime.now(UTC))
                order.fulfillment_status = "cancelled"
                await session.commit()

        async def payment_after_cancel_lock():
            await cancel_has_lock.wait()
            payment_started.set()
            raw = self.raw(event="race-cancel", tx="race-cancel-tx")
            async with self.sf() as session:
                return await PaymentService(session, secret=self.secret).apply_callback(raw, FakeGateway.sign(raw, self.secret))

        cancel_task = asyncio.create_task(cancel_first())
        await cancel_has_lock.wait()
        payment_task = asyncio.create_task(payment_after_cancel_lock())
        await payment_started.wait()
        release_cancel.set()
        result = await asyncio.wait_for(payment_task, 10)
        await asyncio.wait_for(cancel_task, 10)
        self.assertEqual(result.status, "reconciliation")
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == "ORD-PAYMENT-1"))
            balance = await session.scalar(select(InventoryBalance))
            self.assertEqual((order.fulfillment_status, order.payment_status), ("cancelled", "paid"))
            self.assertEqual(balance.reserved, 0)
            self.assertEqual(await session.scalar(select(func.count()).select_from(StockMovement).where(StockMovement.movement_type == "release")), 1)

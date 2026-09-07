"""Deterministic PostgreSQL acceptance coverage for fulfillment transitions."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def guarded(value: str | None) -> str:
    if not value:
        raise unittest.SkipTest("TEST_DATABASE_URL must be set")
    parsed = make_url(value)
    if parsed.host not in {"localhost", "127.0.0.1", "::1"} or not (parsed.database or "").endswith("_test"):
        raise RuntimeError("refusing non-local *_test database")
    return value


class FulfillmentPGTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(guarded(os.getenv("TEST_DATABASE_URL")), poolclass=NullPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE payment_events,payment_reconciliations,fulfillment_transitions,payment_attempts,stock_movements,reservations,"
                    "order_items,inventory_balances,products,outbox,idempotency_keys,orders,users RESTART IDENTITY CASCADE"
                )
            )
        from backend.models import InventoryBalance, Product, User

        async with self.sf() as session:
            owner = User(username="fulfillment-owner", password_hash="x", role="owner", is_active=True, token_version=0)
            warehouse = User(username="fulfillment-warehouse", password_hash="x", role="warehouse", is_active=True, token_version=0)
            session.add_all([owner, warehouse])
            await session.flush()
            product = Product(sku="FULFILL-A", name="Fulfillment A", unit_price=100, currency="IRR", is_active=True)
            session.add(product)
            await session.flush()
            session.add(InventoryBalance(product_id=product.id, on_hand=5, reserved=0))
            await session.commit()
            self.owner_id, self.warehouse_id = owner.id, warehouse.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def order(self, key: str):
        from backend.services.orders import Actor, CartLine, CreateCartOrderCommand, OrderService

        async with self.sf() as session:
            result = await OrderService(session).create_cart_order(
                CreateCartOrderCommand("Customer", "09121234567", "Tehran", "Tehran", "Address", None, (CartLine("FULFILL-A", 2),), key),
                Actor(self.owner_id, "owner"),
            )
            return result.order.public_id

    async def test_repeated_cancel_releases_once_and_invalid_rolls_back(self) -> None:
        from backend.models import FulfillmentTransition, InventoryBalance, Order, Reservation, StockMovement
        from backend.services.fulfillment import FulfillmentService, InvalidFulfillmentTransition
        from backend.services.orders import Actor

        public_id = await self.order("cancel")
        async with self.sf() as session:
            service = FulfillmentService(session)
            order = await session.scalar(select(Order).where(Order.public_id == public_id))
            balance = await session.scalar(select(InventoryBalance))
            reservation = await session.scalar(select(Reservation))
            before = (order.fulfillment_status, reservation.status, balance.on_hand, balance.reserved)
            before_counts = (
                await session.scalar(select(func.count(Order.id))),
                await session.scalar(select(func.count(Reservation.id))),
                await session.scalar(select(func.count(InventoryBalance.product_id))),
                await session.scalar(select(func.count(StockMovement.id))),
                await session.scalar(select(func.count(FulfillmentTransition.id))),
            )
            with self.assertRaises(InvalidFulfillmentTransition):
                await service.transition(public_id, "shipped", Actor(self.owner_id, "owner"), "invalid jump")
            await session.rollback()
            order = await session.scalar(select(Order).where(Order.public_id == public_id))
            balance = await session.scalar(select(InventoryBalance))
            reservation = await session.scalar(select(Reservation))
            self.assertEqual((order.fulfillment_status, reservation.status, balance.on_hand, balance.reserved), before)
            after_counts = (
                await session.scalar(select(func.count(Order.id))),
                await session.scalar(select(func.count(Reservation.id))),
                await session.scalar(select(func.count(InventoryBalance.product_id))),
                await session.scalar(select(func.count(StockMovement.id))),
                await session.scalar(select(func.count(FulfillmentTransition.id))),
            )
            self.assertEqual(after_counts, before_counts)
            first = await service.cancel_order(public_id, Actor(self.owner_id, "owner"))
            second = await service.cancel_order(public_id, Actor(self.owner_id, "owner"))
            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == public_id))
            balance = await session.scalar(select(InventoryBalance))
            self.assertEqual(order.fulfillment_status, "cancelled")
            self.assertEqual(balance.reserved, 0)
            self.assertEqual(await session.scalar(select(func.count(StockMovement.id)).where(StockMovement.movement_type == "release")), 1)
            self.assertEqual(await session.scalar(select(func.count(FulfillmentTransition.id))), 1)
            with self.assertRaises(InvalidFulfillmentTransition):
                await FulfillmentService(session).transition(public_id, "packing", Actor(self.owner_id, "owner"), "bad reverse")
            await session.rollback()
            reservation = await session.scalar(select(Reservation))
            self.assertEqual(reservation.status, "released")

    async def test_shipping_consumes_reserved_stock_once_and_rejects_cancel(self) -> None:
        from backend.models import InventoryBalance, Reservation, StockMovement
        from backend.services.fulfillment import FulfillmentService, InvalidFulfillmentTransition
        from backend.services.orders import Actor

        public_id = await self.order("ship")
        owner = Actor(self.owner_id, "owner")
        warehouse = Actor(self.warehouse_id, "warehouse")
        async with self.sf() as session:
            service = FulfillmentService(session)
            await service.transition(public_id, "confirmed", owner, "confirmed")
            await service.transition(public_id, "packing", warehouse, "packed")
            first = await service.transition(public_id, "shipped", warehouse, "shipped")
            second = await service.transition(public_id, "shipped", warehouse, "replayed")
            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            with self.assertRaises(InvalidFulfillmentTransition):
                await service.cancel_order(public_id, owner)
            await session.rollback()
        async with self.sf() as session:
            balance = await session.scalar(select(InventoryBalance))
            reservation = await session.scalar(select(Reservation))
            self.assertEqual((balance.on_hand, balance.reserved, reservation.status), (3, 0, "consumed"))
            self.assertEqual(await session.scalar(select(func.count(StockMovement.id)).where(StockMovement.movement_type == "consume")), 1)

    async def test_expiry_releases_once(self) -> None:
        from backend.models import FulfillmentTransition, InventoryBalance, Order, Reservation, StockMovement
        from backend.services.fulfillment import FulfillmentService, InvalidFulfillmentTransition
        from backend.services.orders import Actor

        public_id = await self.order("expiry")
        async with self.sf() as session:
            before_counts = (
                await session.scalar(select(func.count(Order.id))),
                await session.scalar(select(func.count(Reservation.id))),
                await session.scalar(select(func.count(InventoryBalance.product_id))),
                await session.scalar(select(func.count(StockMovement.id))),
                await session.scalar(select(func.count(FulfillmentTransition.id))),
            )
            with self.assertRaises(InvalidFulfillmentTransition):
                await FulfillmentService(session).expire_order(public_id, Actor(self.owner_id, "owner"))
            await session.rollback()
            after_counts = (
                await session.scalar(select(func.count(Order.id))),
                await session.scalar(select(func.count(Reservation.id))),
                await session.scalar(select(func.count(InventoryBalance.product_id))),
                await session.scalar(select(func.count(StockMovement.id))),
                await session.scalar(select(func.count(FulfillmentTransition.id))),
            )
            self.assertEqual(after_counts, before_counts)
            reservation = await session.scalar(select(Reservation))
            reservation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()
        async with self.sf() as session:
            service = FulfillmentService(session)
            first = await service.expire_order(public_id, Actor(self.owner_id, "owner"), at=datetime.now(UTC))
            second = await service.expire_order(public_id, Actor(self.owner_id, "owner"), at=datetime.now(UTC))
            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == public_id))
            reservation = await session.scalar(select(Reservation))
            balance = await session.scalar(select(InventoryBalance))
            self.assertEqual(order.fulfillment_status, "expired")
            self.assertEqual(reservation.status, "expired")
            self.assertEqual(balance.reserved, 0)
            self.assertEqual(await session.scalar(select(func.count(StockMovement.id)).where(StockMovement.movement_type == "release")), 1)
            self.assertEqual(await session.scalar(select(func.count(FulfillmentTransition.id))), 1)

    async def test_paid_cancellation_requires_reconciliation_without_refund(self) -> None:
        from backend.models import Order, PaymentAttempt, PaymentReconciliation
        from backend.services.fulfillment import FulfillmentService
        from backend.services.orders import Actor

        public_id = await self.order("paid-cancel")
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == public_id))
            order.payment_status = "paid"
            session.add(PaymentAttempt(order_id=order.id, amount=order.amount, currency="IRR", status="paid", provider="fake"))
            await session.commit()
        async with self.sf() as session:
            service = FulfillmentService(session)
            await service.cancel_order(public_id, Actor(self.owner_id, "owner"), "customer changed mind")
            await service.cancel_order(public_id, Actor(self.owner_id, "owner"), "replayed cancellation")
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == public_id))
            self.assertTrue(order.reconciliation_required)
            self.assertEqual(order.payment_status, "paid")
            self.assertNotEqual(order.payment_status, "refunded")
            reconciliation = await session.scalar(select(PaymentReconciliation).where(PaymentReconciliation.order_id == order.id))
            self.assertIsNotNone(reconciliation)
            self.assertEqual(
                (reconciliation.kind, reconciliation.status, reconciliation.amount, reconciliation.currency),
                ("refund_required", "open", order.amount, "IRR"),
            )
            self.assertIsNotNone(reconciliation.payment_attempt_id)
            self.assertEqual(await session.scalar(select(func.count(PaymentReconciliation.id)).where(PaymentReconciliation.order_id == order.id)), 1)

    async def test_service_requires_active_actor_and_exact_role_for_expiry(self) -> None:
        from backend.models import User
        from backend.services.fulfillment import FulfillmentService
        from backend.services.orders import Actor

        public_id = await self.order("authorization")
        async with self.sf() as session:
            owner = await session.get(User, self.owner_id)
            owner.is_active = False
            await session.commit()
        async with self.sf() as session:
            service = FulfillmentService(session)
            with self.assertRaises(PermissionError):
                await service.transition(public_id, "confirmed", Actor(self.owner_id, "owner"), "inactive")
            with self.assertRaises(PermissionError):
                await service.expire_order(public_id, Actor(self.owner_id, "owner"))
            with self.assertRaises(PermissionError):
                await service.transition(public_id, "confirmed", Actor(self.owner_id, "manager"), "wrong role")
            with self.assertRaises(PermissionError):
                await service.expire_order(public_id, Actor(self.warehouse_id, "warehouse"))
            await session.rollback()
        async with self.sf() as session:
            owner = await session.get(User, self.owner_id)
            owner.is_active = True
            await session.commit()

    async def test_catalog_reservation_expiry_is_persisted(self) -> None:
        from backend.models import Order, Reservation

        public_id = await self.order("expires-at")
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == public_id))
            reservation = await session.scalar(select(Reservation).where(Reservation.order_id == order.id))
            self.assertIsNotNone(reservation.expires_at)
            self.assertGreater(reservation.expires_at, order.created_at)

    async def test_payment_races_with_expiry_and_cancel_follow_order_lock_policy(self) -> None:
        from backend.models import FulfillmentTransition, InventoryBalance, Order, PaymentReconciliation, Reservation, StockMovement
        from backend.services.fulfillment import FulfillmentService
        from backend.services.orders import Actor
        from backend.services.payments import FakeGateway, PaymentService

        async def race_expiry() -> None:
            public_id = await self.order("race-expiry")
            async with self.sf() as session:
                reservation = await session.scalar(select(Reservation))
                reservation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
                await session.commit()
            barrier = asyncio.Barrier(2)

            async def pay() -> None:
                async with self.sf() as session:
                    await barrier.wait()
                    raw = FakeGateway.payload(
                        order_id=public_id,
                        amount=200,
                        provider_event_id="race-expiry-event",
                        provider_transaction_id="race-expiry-tx",
                    )
                    await PaymentService(session, secret="race-secret").apply_callback(
                        raw, FakeGateway.sign(raw, "race-secret")
                    )

            async def expire() -> None:
                async with self.sf() as session:
                    await barrier.wait()
                    await FulfillmentService(session).expire_order(public_id, Actor(self.owner_id, "owner"), at=datetime.now(UTC))

            await asyncio.gather(pay(), expire())
            async with self.sf() as session:
                order = await session.scalar(select(Order).where(Order.public_id == public_id))
                self.assertEqual(order.fulfillment_status, "expired")
                self.assertEqual(
                    await session.scalar(
                        select(func.count(StockMovement.id)).where(StockMovement.order_id == order.id, StockMovement.movement_type == "release")
                    ),
                    1,
                )
                self.assertEqual(await session.scalar(select(func.count(FulfillmentTransition.id)).where(FulfillmentTransition.order_id == order.id)), 1)
                self.assertEqual(await session.scalar(select(func.count(PaymentReconciliation.id)).where(PaymentReconciliation.order_id == order.id)), 1)
                balance = await session.scalar(select(InventoryBalance))
                self.assertEqual(balance.reserved, 0)

        async def race_cancel() -> None:
            public_id = await self.order("race-cancel")
            barrier = asyncio.Barrier(2)

            async def pay() -> None:
                async with self.sf() as session:
                    await barrier.wait()
                    raw = FakeGateway.payload(
                        order_id=public_id,
                        amount=200,
                        provider_event_id="race-cancel-event",
                        provider_transaction_id="race-cancel-tx",
                    )
                    await PaymentService(session, secret="race-secret").apply_callback(
                        raw, FakeGateway.sign(raw, "race-secret")
                    )

            async def cancel() -> None:
                async with self.sf() as session:
                    await barrier.wait()
                    await FulfillmentService(session).cancel_order(public_id, Actor(self.owner_id, "owner"), "race cancellation")

            await asyncio.gather(pay(), cancel())
            async with self.sf() as session:
                order = await session.scalar(select(Order).where(Order.public_id == public_id))
                self.assertEqual(order.fulfillment_status, "cancelled")
                self.assertEqual(
                    await session.scalar(
                        select(func.count(StockMovement.id)).where(StockMovement.order_id == order.id, StockMovement.movement_type == "release")
                    ),
                    1,
                )
                self.assertEqual(await session.scalar(select(func.count(PaymentReconciliation.id)).where(PaymentReconciliation.order_id == order.id)), 1)
                self.assertEqual((await session.scalar(select(InventoryBalance))).reserved, 0)

        await race_expiry()
        await race_cancel()


if __name__ == "__main__":
    unittest.main()

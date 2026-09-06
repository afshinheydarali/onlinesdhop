"""Deterministic PostgreSQL acceptance coverage for fulfillment transitions."""

from __future__ import annotations

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
                    "TRUNCATE fulfillment_transitions,payment_attempts,stock_movements,reservations,"
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
        from backend.models import Reservation
        from backend.services.fulfillment import FulfillmentService
        from backend.services.orders import Actor

        public_id = await self.order("expiry")
        async with self.sf() as session:
            reservation = await session.scalar(select(Reservation))
            reservation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()
        async with self.sf() as session:
            service = FulfillmentService(session)
            first = await service.expire_order(public_id, Actor(self.owner_id, "owner"), at=datetime.now(UTC))
            second = await service.expire_order(public_id, Actor(self.owner_id, "owner"), at=datetime.now(UTC))
            self.assertTrue(first.changed)
            self.assertFalse(second.changed)


if __name__ == "__main__":
    unittest.main()

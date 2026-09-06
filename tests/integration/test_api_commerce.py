import os
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

DB = os.getenv("TEST_DATABASE_URL")


class CommerceAPIPGTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not DB:
            raise unittest.SkipTest("TEST_DATABASE_URL must be set")
        url = make_url(DB)
        if url.host not in {"localhost", "127.0.0.1", "::1"} or not (url.database or "").endswith("_test"):
            raise RuntimeError("refusing non-local *_test database")
        self.engine = create_async_engine(DB, poolclass=NullPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.models import Admin, IdempotencyKey, InventoryBalance, Order, OrderItem, Outbox, Product, Reservation, StockMovement, User

        async with self.engine.begin() as c:
            await c.execute(delete(StockMovement))
            await c.execute(delete(Reservation))
            await c.execute(delete(OrderItem))
            await c.execute(delete(InventoryBalance))
            await c.execute(delete(Product))
            await c.execute(delete(Outbox))
            await c.execute(delete(IdempotencyKey))
            await c.execute(delete(Order))
            await c.execute(delete(Admin))
            await c.execute(delete(User))
        from backend.auth import hash_password

        async with self.sf() as s:
            s.add_all(
                [
                    User(username=f"commerce-{role}", password_hash=hash_password("correct horse battery staple"), role=role, is_active=True, token_version=0)
                    for role in ("owner", "manager", "seller", "warehouse")
                ]
            )
            await s.commit()
        from backend.api.app import app

        self.patches = [patch("backend.api.app.SessionFactory", self.sf), patch("backend.auth.SessionFactory", self.sf)]
        for p in self.patches:
            p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in self.patches:
            p.stop()
        await self.engine.dispose()

    async def auth(self, role):
        r = await self.client.post("/api/v1/auth/token", data={"username": f"commerce-{role}", "password": "correct horse battery staple"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    async def test_roles_privacy_totals_snapshots_and_strict_payload(self):
        owner, manager, seller, warehouse = [await self.auth(r) for r in ("owner", "manager", "seller", "warehouse")]
        self.assertEqual((await self.client.get("/api/v1/products")).status_code, 401)
        self.assertEqual(
            (await self.client.post("/api/v1/products", json={"sku": "a", "name": "A", "unit_price": 120, "on_hand": 3}, headers=warehouse)).status_code, 403
        )
        created = await self.client.post("/api/v1/products", json={"sku": " a ", "name": "A", "unit_price": 120, "on_hand": 3}, headers=manager)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["sku"], "A")
        self.assertEqual(
            (await self.client.post("/api/v1/products", json={"sku": "b", "name": "B", "unit_price": 1, "currency": "USD"}, headers=owner)).status_code, 422
        )
        payload = {
            "customer_name": "Secret",
            "phone_raw": "09121234567",
            "province": "T",
            "city": "T",
            "address": "Secret address",
            "items": [{"sku": "A", "quantity": 2}],
            "idempotency_key": "api-commerce",
        }
        self.assertEqual((await self.client.post("/api/v1/commerce/orders", json=payload | {"amount": 1}, headers=seller)).status_code, 422)
        self.assertEqual((await self.client.post("/api/v1/commerce/orders", json=payload | {"currency": "IRR"}, headers=seller)).status_code, 422)
        self.assertEqual((await self.client.post("/api/v1/commerce/orders", json=payload | {"unexpected": 1}, headers=seller)).status_code, 422)
        self.assertEqual((await self.client.post("/api/v1/commerce/orders", json=payload, headers=warehouse)).status_code, 403)
        response = await self.client.post("/api/v1/commerce/orders", json=payload, headers=seller)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("amount", response.json())
        self.assertNotIn("address", response.json())
        async with self.sf() as s:
            from backend.models import IdempotencyKey, Order, OrderItem, Outbox, Reservation, StockMovement

            order = await s.scalar(select(Order).where(Order.public_id == response.json()["public_id"]))
            self.assertEqual((order.amount, order.currency), (240, "IRR"))
            self.assertEqual(await s.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.order_id == order.id)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(Reservation).where(Reservation.order_id == order.id)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(StockMovement).where(StockMovement.order_id == order.id)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.order_id == order.id)), 1)
            self.assertEqual(await s.scalar(select(func.count()).select_from(Outbox).where(Outbox.order_id == order.id)), 1)

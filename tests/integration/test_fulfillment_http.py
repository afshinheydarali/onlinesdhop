"""HTTP authorization and privacy acceptance for fulfillment/report routes."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("JWT_SECRET", "fulfillment-http-test-secret-32-characters")


def guarded(value: str | None) -> str:
    if not value:
        raise unittest.SkipTest("TEST_DATABASE_URL must be set")
    parsed = make_url(value)
    if parsed.host not in {"localhost", "127.0.0.1", "::1"} or not (parsed.database or "").endswith("_test"):
        raise RuntimeError("refusing non-local *_test database")
    return value


class FulfillmentHTTPTests(unittest.IsolatedAsyncioTestCase):
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
        from backend.auth import hash_password
        from backend.models import User

        async with self.sf() as session:
            hashed = hash_password("correct horse battery staple")
            users = [
                User(username=f"http-{role}", password_hash=hashed, role=role, is_active=True, token_version=0)
                for role in ("owner", "manager", "seller", "warehouse")
            ]
            session.add_all(users)
            await session.commit()
            self.ids = {user.role: user.id for user in users}
        from backend.api.app import app

        self.patches = [patch("backend.api.app.SessionFactory", self.sf), patch("backend.auth.SessionFactory", self.sf)]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for item in self.patches:
            item.stop()
        await self.engine.dispose()

    async def auth(self, role: str) -> dict[str, str]:
        response = await self.client.post("/api/v1/auth/token", data={"username": f"http-{role}", "password": "correct horse battery staple"})
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def make_order(self) -> str:
        from backend.services.orders import Actor, CreateOrderCommand, OrderService
        from order_bot.validation import normalize_phone, normalize_product

        phone = "09121234567"
        product = "http widget"
        async with self.sf() as session:
            result = await OrderService(session).create_order(
                CreateOrderCommand(
                    "Secret Customer",
                    phone,
                    normalize_phone(phone),
                    "Tehran",
                    "Tehran",
                    "Private Address",
                    None,
                    product,
                    normalize_product(product),
                    1,
                    100,
                    None,
                    "",
                    "http-order",
                ),
                Actor(self.ids["owner"], "owner"),
            )
            return result.order.public_id

    async def test_http_role_matrix_privacy_and_report_endpoints(self) -> None:
        owner, manager, seller, warehouse = [await self.auth(role) for role in ("owner", "manager", "seller", "warehouse")]
        public_id = await self.make_order()
        self.assertEqual((await self.client.get(f"/api/v1/orders/{public_id}/fulfillment")).status_code, 401)
        self.assertEqual((await self.client.get(f"/api/v1/orders/{public_id}/fulfillment", headers=seller)).status_code, 403)
        self.assertEqual(
            (await self.client.patch(f"/api/v1/orders/{public_id}/fulfillment", json={"status": "confirmed", "reason": "approved"}, headers=owner)).status_code,
            200,
        )
        self.assertEqual(
            (await self.client.patch(f"/api/v1/orders/{public_id}/fulfillment", json={"status": "packing", "reason": "packed"}, headers=warehouse)).status_code,
            200,
        )
        self.assertEqual(
            (
                await self.client.patch(f"/api/v1/orders/{public_id}/fulfillment", json={"status": "cancelled", "reason": "cancel"}, headers=warehouse)
            ).status_code,
            403,
        )
        warehouse_view = await self.client.get(f"/api/v1/orders/{public_id}", headers=warehouse)
        self.assertEqual(warehouse_view.status_code, 200)
        self.assertNotIn("payment_status", warehouse_view.json())
        self.assertNotIn("amount", warehouse_view.json())
        self.assertEqual((await self.client.get(f"/api/v1/orders/{public_id}", headers=seller)).status_code, 404)
        start = "2026-01-01T00:00:00Z"
        end = "2026-01-02T00:00:00Z"
        for headers in (owner, manager):
            self.assertEqual((await self.client.get("/api/v1/reports/revenue", params={"start": start, "end": end}, headers=headers)).status_code, 200)
            self.assertEqual((await self.client.get("/api/v1/reports/revenue.csv", params={"start": start, "end": end}, headers=headers)).status_code, 200)
        for headers in (seller, warehouse):
            self.assertEqual((await self.client.get("/api/v1/reports/revenue", params={"start": start, "end": end}, headers=headers)).status_code, 403)
            self.assertEqual((await self.client.get("/api/v1/reports/revenue.csv", params={"start": start, "end": end}, headers=headers)).status_code, 403)


if __name__ == "__main__":
    unittest.main()

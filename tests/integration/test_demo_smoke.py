from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


class DemoSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        raw = os.getenv("TEST_DATABASE_URL")
        if not raw:
            self.skipTest("TEST_DATABASE_URL must target a local *_test database")
        from sqlalchemy.engine import make_url

        url = make_url(raw)
        if (url.host not in {"localhost", "127.0.0.1", "::1"} or not url.database
                or not url.database.endswith("_test") or url.database.endswith("_restore_test")):
            raise RuntimeError("test_demo_smoke requires a local database ending in _test")
        seed_env = os.environ.copy()
        subprocess.run([sys.executable, "-m", "scripts.seed_synthetic", "--reset"], check=True, env=seed_env)
        self.engine = create_async_engine(raw, poolclass=NullPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        # Seed is intentionally a subprocess in the documented demo; this test
        # only verifies the real PostgreSQL/API path and assumes migrations ran.
        async with self.engine.begin() as conn:
            if not await conn.scalar(text("SELECT to_regclass('public.products')")):
                self.skipTest("schema is absent; run alembic upgrade head first")
        from backend.api.app import app

        self.api_patch = patch("backend.api.app.SessionFactory", self.sf)
        self.auth_patch = patch("backend.auth.SessionFactory", self.sf)
        self.api_patch.start()
        self.auth_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.api_patch.stop()
        self.auth_patch.stop()
        await self.engine.dispose()

    async def test_api_demo_flow_and_safe_observability(self) -> None:
        live = await self.client.get("/health/live", headers={"X-Request-ID": "demo-correlation-001"})
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.headers.get("X-Request-ID"), "demo-correlation-001")
        self.assertEqual((await self.client.get("/health/ready")).status_code, 200)
        token = await self.client.post("/api/v1/auth/token", data={"username": "portfolio-seller", "password": "portfolio-test-password"})
        self.assertEqual(token.status_code, 200)
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        products = await self.client.get("/api/v1/products", headers=headers)
        self.assertEqual(products.status_code, 200)
        self.assertGreaterEqual(len(products.json()["items"]), 1)
        created = await self.client.post(
            "/api/v1/commerce/orders",
            headers=headers,
            json={
                "customer_name": "Synthetic Customer",
                "phone_raw": "09120009999",
                "province": "Tehran",
                "city": "Tehran",
                "address": "Synthetic Street",
                "items": [{"sku": "DEMO-RED", "quantity": 1}],
                "idempotency_key": "demo-smoke-order",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        order_id = created.json()["public_id"]
        self.assertEqual((await self.client.get(f"/api/v1/orders/{order_id}", headers=headers)).status_code, 200)
        # Operational data is protected even though liveness/readiness remain
        # public. Check both anonymous and authenticated role boundaries.
        self.assertEqual((await self.client.get("/metrics")).status_code, 401)
        owner_token = await self.client.post(
            "/api/v1/auth/token",
            data={"username": "portfolio-owner", "password": "portfolio-test-password"},
        )
        self.assertEqual(owner_token.status_code, 200)
        owner_headers = {"Authorization": f"Bearer {owner_token.json()['access_token']}"}
        self.assertEqual((await self.client.get("/metrics", headers=headers)).status_code, 403)
        owner_order = await self.client.get(f"/api/v1/orders/{order_id}", headers=owner_headers)
        self.assertEqual(owner_order.status_code, 200)
        order_amount = owner_order.json()["amount"]

        # Exercise the real outbox worker with a deterministic transport fault,
        # then make the retry due and recover it with the same worker.
        from backend.models import Order, Outbox
        from backend.services.delivery import DeliveryWorker, TransientDeliveryError

        class DemoTransport:
            def __init__(self) -> None:
                self.failed = False
                self.photos = 0
                self.texts = 0

            async def send_photo(self, order) -> int:
                self.photos += 1
                return 900 + self.photos

            async def send_text(self, order, photo_message_id: int) -> int:
                self.texts += 1
                if not self.failed:
                    self.failed = True
                    raise TransientDeliveryError("injected demo failure", retry_after=0)
                return 950 + self.texts

        transport = DemoTransport()
        worker = DeliveryWorker(self.sf, transport, worker_id="demo-smoke-worker")
        self.assertTrue(await worker.run_once())
        self.assertEqual((transport.photos, transport.texts), (1, 1))
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == order_id))
            outbox = await session.scalar(select(Outbox).where(Outbox.order_id == order.id))
            self.assertIsNotNone(outbox)
            self.assertEqual(outbox.status, "pending")
            self.assertEqual(outbox.error_code, "transient")
            self.assertIsNotNone(outbox.next_attempt_at)
            outbox.next_attempt_at = datetime.now(UTC)
            await session.commit()
        self.assertTrue(await worker.run_once())
        async with self.sf() as session:
            order = await session.scalar(select(Order).where(Order.public_id == order_id))
            outbox = await session.scalar(select(Outbox).where(Outbox.order_id == order.id))
            self.assertEqual(outbox.status, "sent")

        # The signed fake callback is verified over the exact bytes and replay
        # is accepted without a second payment effect.
        from backend.services.payments import FakeGateway

        raw = FakeGateway.payload(
            order_id=order_id,
            amount=order_amount,
            provider_transaction_id="demo-transaction-001",
            provider_event_id="demo-event-001",
        )
        with patch.dict(os.environ, {"FAKE_PAYMENT_HMAC_SECRET": "demo-local-secret"}):
            payment_headers = {"X-Fake-Gateway-Signature": FakeGateway.sign(raw, "demo-local-secret")}
            paid = await self.client.post("/api/v1/payments/fake/callback", content=raw, headers=payment_headers)
            replay = await self.client.post("/api/v1/payments/fake/callback", content=raw, headers=payment_headers)
        self.assertEqual(paid.status_code, 200, paid.text)
        self.assertTrue(paid.json()["applied"])
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertFalse(replay.json()["applied"])

        metrics = await self.client.get("/metrics", headers=owner_headers)
        self.assertEqual(metrics.status_code, 200)
        body = metrics.json()
        self.assertIn("http", body)
        self.assertIn("delivery", body)
        self.assertNotIn("Synthetic Street", metrics.text)


if __name__ == "__main__":
    unittest.main()

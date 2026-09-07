from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


class DemoSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        raw = os.getenv("TEST_DATABASE_URL")
        if not raw:
            self.skipTest("TEST_DATABASE_URL must target the dedicated local portfolio test DB")
        from sqlalchemy.engine import make_url

        url = make_url(raw)
        if url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "onlineshop_portfolio_test":
            raise RuntimeError("test_demo_smoke requires local onlineshop_portfolio_test only")
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
        live = await self.client.get("/health/live")
        self.assertEqual(live.status_code, 200)
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
        metrics = await self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        body = metrics.json()
        self.assertIn("http", body)
        self.assertIn("delivery", body)
        self.assertNotIn("Synthetic Street", metrics.text)


if __name__ == "__main__":
    unittest.main()

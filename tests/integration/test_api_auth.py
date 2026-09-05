import os
import unittest
import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

os.environ.setdefault("JWT_SECRET", "integration-test-secret-32-characters-long")
DB = os.getenv("TEST_DATABASE_URL")

class APIAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not DB or "_test" not in DB: self.skipTest("TEST_DATABASE_URL must explicitly target *_test")
        self.engine = create_async_engine(DB, poolclass=NullPool); self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.models import IdempotencyKey, Order, Outbox, User
        async with self.engine.begin() as c:
            await c.execute(delete(Outbox)); await c.execute(delete(IdempotencyKey)); await c.execute(delete(Order)); await c.execute(delete(User))
        from backend.auth import hash_password
        async with self.sf() as s:
            s.add_all([User(username="api-owner", password_hash=hash_password("correct horse battery staple"), role="owner", token_version=0), User(username="api-seller", password_hash=hash_password("correct horse battery staple"), role="seller", token_version=0)]); await s.commit()
        from backend.api.app import app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self): await self.client.aclose(); await self.engine.dispose()

    async def test_auth_validation_and_seller_privacy(self):
        response = await self.client.get("/api/v1/orders"); self.assertEqual(response.status_code, 401)
        token = await self.client.post("/api/v1/auth/token", data={"username": "api-seller", "password": "correct horse battery staple"}); self.assertEqual(token.status_code, 200)
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        payload = {"customer_name":"Secret Name","phone_raw":"09121234567","province":"Tehran","city":"Tehran","address":"Secret Address","product_raw":"Widget","quantity":1,"amount":100,"idempotency_key":"api-1"}
        created = await self.client.post("/api/v1/orders", json=payload, headers=headers); self.assertEqual(created.status_code, 200)
        body = created.json(); self.assertNotIn("customer_name", body); self.assertNotIn("phone_raw", body); self.assertNotIn("address", body)
        bad = dict(payload); bad["unexpected"] = 1; bad["idempotency_key"] = "api-2"; self.assertEqual((await self.client.post("/api/v1/orders", json=bad, headers=headers)).status_code, 422)

if __name__ == "__main__": unittest.main()

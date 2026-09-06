import os
import unittest
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("JWT_SECRET", "integration-test-secret-32-characters-long")
DB = os.getenv("TEST_DATABASE_URL")


class APIAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not DB:
            self.skipTest("TEST_DATABASE_URL must explicitly target *_test")
        guarded = make_url(DB)
        if not (guarded.database or "").endswith("_test") or guarded.host not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise RuntimeError("refusing non-local *_test database")
        self.engine = create_async_engine(DB, poolclass=NullPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from backend.models import IdempotencyKey, Order, Outbox, User

        async with self.engine.begin() as c:
            await c.execute(delete(Outbox))
            await c.execute(delete(IdempotencyKey))
            await c.execute(delete(Order))
            await c.execute(delete(User))
        from backend.auth import hash_password

        async with self.sf() as s:
            users = [
                User(
                    username="api-owner",
                    password_hash=hash_password("correct horse battery staple"),
                    role="owner",
                    token_version=0,
                ),
                User(
                    username="api-seller",
                    password_hash=hash_password("correct horse battery staple"),
                    role="seller",
                    token_version=0,
                ),
                User(
                    username="api-warehouse",
                    password_hash=hash_password("correct horse battery staple"),
                    role="warehouse",
                    token_version=0,
                ),
            ]
            s.add_all(users)
            await s.flush()
            self.seller_id = users[1].id
            await s.commit()
        import backend.api.app as api_module
        import backend.auth as auth_module
        from backend.api.app import app

        api_module.SessionFactory = self.sf
        auth_module.SessionFactory = self.sf

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def test_auth_validation_and_seller_privacy(self):
        response = await self.client.get("/api/v1/orders")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            (
                await self.client.get(
                    "/api/v1/orders", headers={"Authorization": "Bearer malformed"}
                )
            ).status_code,
            401,
        )
        secret = os.environ["JWT_SECRET"]
        expired = jwt.encode(
            {
                "sub": "2",
                "exp": datetime.now(UTC) - timedelta(minutes=1),
                "iat": datetime.now(UTC),
                "token_version": 0,
            },
            secret,
            algorithm="HS256",
        )
        self.assertEqual(
            (
                await self.client.get(
                    "/api/v1/orders", headers={"Authorization": f"Bearer {expired}"}
                )
            ).status_code,
            401,
        )
        token = await self.client.post(
            "/api/v1/auth/token",
            data={"username": "api-seller", "password": "correct horse battery staple"},
        )
        self.assertEqual(token.status_code, 200)
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        payload = {
            "customer_name": "Secret Name",
            "phone_raw": "09121234567",
            "province": "Tehran",
            "city": "Tehran",
            "address": "Secret Address",
            "product_raw": "Widget",
            "quantity": 1,
            "amount": 100,
            "idempotency_key": "api-1",
        }
        created = await self.client.post(
            "/api/v1/orders", json=payload, headers=headers
        )
        self.assertEqual(created.status_code, 200)
        body = created.json()
        self.assertNotIn("customer_name", body)
        self.assertNotIn("phone_raw", body)
        self.assertNotIn("address", body)
        bad = dict(payload)
        bad["unexpected"] = 1
        bad["idempotency_key"] = "api-2"
        self.assertEqual(
            (
                await self.client.post("/api/v1/orders", json=bad, headers=headers)
            ).status_code,
            422,
        )
        warehouse = await self.client.post(
            "/api/v1/auth/token",
            data={
                "username": "api-warehouse",
                "password": "correct horse battery staple",
            },
        )
        warehouse_headers = {
            "Authorization": f"Bearer {warehouse.json()['access_token']}"
        }
        self.assertEqual(
            (
                await self.client.post(
                    "/api/v1/orders",
                    json=payload | {"idempotency_key": "api-warehouse"},
                    headers=warehouse_headers,
                )
            ).status_code,
            403,
        )

    async def test_revocation_and_inactive_identity(self):
        token = await self.client.post(
            "/api/v1/auth/token",
            data={"username": "api-seller", "password": "correct horse battery staple"},
        )
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        owner = await self.client.post(
            "/api/v1/auth/token",
            data={"username": "api-owner", "password": "correct horse battery staple"},
        )
        owner_headers = {"Authorization": f"Bearer {owner.json()['access_token']}"}
        self.assertEqual(
            (
                await self.client.patch(
                    f"/api/v1/users/{self.seller_id}/revoke", headers=owner_headers
                )
            ).status_code,
            200,
        )
        self.assertEqual(
            (await self.client.get("/api/v1/orders", headers=headers)).status_code, 401
        )


if __name__ == "__main__":
    unittest.main()

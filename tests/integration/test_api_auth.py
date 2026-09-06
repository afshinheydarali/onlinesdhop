import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import jwt
from sqlalchemy import delete, select
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
        from backend.models import Admin, IdempotencyKey, Order, Outbox, User

        async with self.engine.begin() as c:
            await c.execute(delete(Outbox))
            await c.execute(delete(IdempotencyKey))
            await c.execute(delete(Order))
            await c.execute(delete(Admin))
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
                    username="api-seller2",
                    password_hash=hash_password("correct horse battery staple"),
                    role="seller",
                    token_version=0,
                ),
                User(
                    username="api-manager",
                    password_hash=hash_password("correct horse battery staple"),
                    role="manager",
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
            self.owner_id = users[0].id
            self.seller2_id = users[2].id
            self.manager_id = users[3].id
            await s.commit()
        from backend.models import Admin

        async with self.sf() as s:
            s.add(
                Admin(
                    telegram_id=9001,
                    name="API Admin",
                    admin_code="API01",
                    is_active=True,
                    created_at=datetime.now(UTC),
                )
            )
            await s.commit()
        from backend.api.app import app

        self.api_factory_patch = patch("backend.api.app.SessionFactory", self.sf)
        self.auth_factory_patch = patch("backend.auth.SessionFactory", self.sf)
        self.api_factory_patch.start()
        self.auth_factory_patch.start()

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.auth_factory_patch.stop()
        self.api_factory_patch.stop()
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

    async def test_finite_role_route_matrix_and_ownership(self):
        async def auth(username):
            response = await self.client.post(
                "/api/v1/auth/token",
                data={"username": username, "password": "correct horse battery staple"},
            )
            self.assertEqual(response.status_code, 200)
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        users = {
            "owner": await auth("api-owner"),
            "manager": await auth("api-manager"),
            "seller": await auth("api-seller"),
            "warehouse": await auth("api-warehouse"),
        }
        seller_order = await self.client.post(
            "/api/v1/orders",
            json={
                "customer_name": "Customer",
                "phone_raw": "09121234567",
                "province": "Tehran",
                "city": "Tehran",
                "address": "Street",
                "product_raw": "Widget",
                "quantity": 1,
                "idempotency_key": "matrix-seed",
                "allow_duplicate": True,
            },
            headers=users["seller"],
        )
        self.assertEqual(seller_order.status_code, 200)
        public_id = seller_order.json()["public_id"]
        protected = [
            (
                "GET orders",
                "GET",
                "/api/v1/orders",
                {"limit": 10},
                {"owner", "manager", "seller", "warehouse"},
            ),
            (
                "GET order",
                "GET",
                f"/api/v1/orders/{public_id}",
                None,
                {"owner", "manager", "seller", "warehouse"},
            ),
            (
                "POST order",
                "POST",
                "/api/v1/orders",
                {
                    "customer_name": "C",
                    "phone_raw": "09120000001",
                    "province": "T",
                    "city": "T",
                    "address": "A",
                    "product_raw": "P",
                    "quantity": 1,
                    "idempotency_key": "matrix-create",
                    "allow_duplicate": True,
                },
                {"owner", "manager", "seller"},
            ),
            (
                "POST admin",
                "POST",
                "/api/v1/admins",
                {"telegram_id": 9100, "name": "N", "admin_code": "M01"},
                {"owner"},
            ),
            (
                "PATCH admin",
                "PATCH",
                "/api/v1/admins/9001",
                {"active": True},
                {"owner"},
            ),
            (
                "POST user",
                "POST",
                "/api/v1/users",
                {
                    "username": "matrix-user",
                    "password": "correct horse battery staple",
                    "role": "seller",
                },
                {"owner"},
            ),
            ("GET users", "GET", "/api/v1/users", {"limit": 10}, {"owner"}),
            (
                "PATCH user",
                "PATCH",
                f"/api/v1/users/{self.owner_id}/revoke",
                None,
                {"owner"},
            ),
        ]
        for label, method, path, body, allowed in protected:
            for role, headers in users.items():
                with self.subTest(route=label, role=role):
                    if method == "GET":
                        response = await self.client.get(
                            path, params=body, headers=headers
                        )
                    elif method == "POST":
                        payload = body | (
                            {"idempotency_key": f"{body['idempotency_key']}-{role}"}
                            if "idempotency_key" in body
                            else {}
                        )
                        response = await self.client.post(
                            path, json=payload, headers=headers
                        )
                    elif body is None:
                        response = await self.client.patch(path, headers=headers)
                    else:
                        response = await self.client.patch(
                            path, json=body, headers=headers
                        )
                    expected = (
                        (
                            201
                            if method == "POST" and label in {"POST admin", "POST user"}
                            else 200
                        )
                        if role in allowed
                        else 403
                    )
                    self.assertEqual(response.status_code, expected)
        seller2 = await auth("api-seller2")
        with self.subTest(route="seller nonowned order"):
            self.assertEqual(
                (
                    await self.client.get(
                        f"/api/v1/orders/{public_id}", headers=seller2
                    )
                ).status_code,
                404,
            )

        warehouse = await self.client.get(
            f"/api/v1/orders/{public_id}", headers=users["warehouse"]
        )
        self.assertEqual(warehouse.status_code, 200)
        self.assertEqual(
            set(warehouse.json()) & {"address", "product_raw", "quantity"},
            {"address", "product_raw", "quantity"},
        )
        self.assertNotIn("amount", warehouse.json())
        self.assertNotIn("delivery_error", warehouse.json())
        async with self.sf() as s:
            from backend.models import Order

            seeded = await s.scalar(select(Order).where(Order.public_id == public_id))
            seeded.delivery_error = (
                "failed for Secret Name at 09121234567, Secret Address"
            )
            await s.commit()
        seller_view = await self.client.get(
            f"/api/v1/orders/{public_id}", headers=users["seller"]
        )
        self.assertEqual(seller_view.status_code, 200)
        self.assertNotIn("09121234567", seller_view.text)
        self.assertNotIn("Secret Address", seller_view.text)

    async def test_claim_validation_password_and_payload_bounds(self):
        bad_password = await self.client.post(
            "/api/v1/auth/token",
            data={"username": "api-owner", "password": "wrong password"},
        )
        self.assertEqual(bad_password.status_code, 401)
        from backend.models import User

        async with self.sf() as s:
            user = await s.scalar(select(User).where(User.username == "api-owner"))
            user.password_hash = "legacy-disabled-hash"
            await s.commit()
        disabled = await self.client.post(
            "/api/v1/auth/token",
            data={"username": "api-owner", "password": "correct horse battery staple"},
        )
        self.assertEqual(disabled.status_code, 401)

        secret = os.environ["JWT_SECRET"]
        now = datetime.now(UTC)
        claims = {
            "sub": str(self.owner_id),
            "exp": now + timedelta(minutes=5),
            "iat": now,
            "token_version": 0,
        }
        claim_cases = [
            ("missing sub", {k: v for k, v in claims.items() if k != "sub"}),
            ("string version", claims | {"token_version": "0"}),
            (
                "missing version",
                {k: v for k, v in claims.items() if k != "token_version"},
            ),
            ("version mismatch", claims | {"token_version": 1}),
        ]
        for label, body in claim_cases:
            with self.subTest(claim=label):
                forged = jwt.encode(body, secret, algorithm="HS256")
                self.assertEqual(
                    (
                        await self.client.get(
                            "/api/v1/users",
                            headers={"Authorization": f"Bearer {forged}"},
                        )
                    ).status_code,
                    401,
                )
        seller_claims = claims | {"sub": str(self.seller_id), "role": "owner"}
        seller_forged = jwt.encode(seller_claims, secret, algorithm="HS256")
        self.assertEqual(
            (
                await self.client.get(
                    "/api/v1/users",
                    headers={"Authorization": f"Bearer {seller_forged}"},
                )
            ).status_code,
            403,
        )
        valid_seller = await self.client.post(
            "/api/v1/auth/token",
            data={"username": "api-seller", "password": "correct horse battery staple"},
        )
        seller_headers = {
            "Authorization": f"Bearer {valid_seller.json()['access_token']}"
        }
        payload = {
            "customer_name": "C",
            "phone_raw": "09129999999",
            "province": "T",
            "city": "T",
            "address": "A",
            "product_raw": "P",
            "quantity": 1,
            "amount": 5,
            "idempotency_key": "bounds-case",
        }
        first = await self.client.post(
            "/api/v1/orders", json=payload, headers=seller_headers
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            (
                await self.client.post(
                    "/api/v1/orders",
                    json=payload | {"amount": 6},
                    headers=seller_headers,
                )
            ).status_code,
            409,
        )
        bad_cases = [
            ("name", {"customer_name": "x" * 121}),
            ("postal", {"postal_code": "x" * 31}),
            ("notes", {"notes": "x" * 1001}),
            ("quantity", {"quantity": 0}),
            ("quantity bool", {"quantity": True}),
            ("amount", {"amount": -1}),
            ("amount bool", {"amount": True}),
            ("huge id", {"phone_raw": "x" * 31}),
        ]
        for label, change in bad_cases:
            with self.subTest(bound=label):
                self.assertEqual(
                    (
                        await self.client.post(
                            "/api/v1/orders",
                            json=payload
                            | change
                            | {
                                "idempotency_key": f"bound-{label}",
                                "allow_duplicate": True,
                            },
                            headers=seller_headers,
                        )
                    ).status_code,
                    422,
                )


if __name__ == "__main__":
    unittest.main()

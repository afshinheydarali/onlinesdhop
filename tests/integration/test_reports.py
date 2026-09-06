"""Deterministic PostgreSQL report arithmetic, boundaries, pagination and CSV safety."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
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


class ReportsPGTests(unittest.IsolatedAsyncioTestCase):
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
        from backend.models import User

        async with self.sf() as session:
            user = User(username="reports-owner", password_hash="x", role="owner", is_active=True, token_version=0)
            session.add(user)
            await session.commit()
            self.owner_id = user.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_paid_arithmetic_boundary_pagination_and_csv_safety(self) -> None:
        from backend.models import Order
        from backend.services.fulfillment import ReportService

        base = datetime(2026, 1, 1, tzinfo=UTC)
        async with self.sf() as session:
            rows = []
            fixtures = (
                ("=2+2", 100, "confirmed", "paid", False),
                ("safe", 200, "shipped", "paid", False),
                ("unpaid", 400, "confirmed", "pending", False),
                ("cancelled", 500, "cancelled", "paid", False),
                ("expired", 600, "expired", "paid", False),
                ("recon", 700, "confirmed", "paid", True),
            )
            for number, amount, state, payment, reconciliation in fixtures:
                rows.append(
                    Order(
                        public_id=number,
                        created_by_id=self.owner_id,
                        customer_name="c",
                        phone_raw="p",
                        phone_normalized=number,
                        province="t",
                        city="t",
                        address="a",
                        product_raw="x",
                        product_normalized="x",
                        quantity=1,
                        amount=amount,
                        currency="IRR",
                        notes=None,
                        photo_file_id="",
                        draft_token=f"token-{number}",
                        created_at=base,
                        delivery_status="pending",
                        fulfillment_status=state,
                        payment_status=payment,
                        reconciliation_required=reconciliation,
                    )
                )
            session.add_all(rows)
            await session.commit()
        async with self.sf() as session:
            service = ReportService(session)
            report = await service.revenue(start=base, end=base + timedelta(days=1), limit=1)
            self.assertEqual((report["order_count"], report["revenue"]), (2, 300))
            self.assertEqual(len(report["items"]), 1)
            page = await service.revenue(start=base, end=base + timedelta(days=1), limit=1, cursor=report["next_cursor"])
            self.assertEqual(len(page["items"]), 1)
            self.assertIsNone((await service.revenue(start=base + timedelta(days=1), end=base + timedelta(days=2)))["next_cursor"])
            body = await service.csv(start=base, end=base + timedelta(days=1))
            self.assertIn("'=2+2", body)
            self.assertNotIn("=2+2", body.replace("'=2+2", ""))


if __name__ == "__main__":
    unittest.main()

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
                    "TRUNCATE payment_reconciliations,fulfillment_transitions,payment_attempts,stock_movements,reservations,"
                    "order_items,inventory_balances,products,outbox,idempotency_keys,orders,users RESTART IDENTITY CASCADE"
                )
            )
        from backend.models import User

        async with self.sf() as session:
            user = User(username="reports-owner", password_hash="x", role="owner", is_active=True, token_version=0)
            seller = User(username="reports-seller", password_hash="x", role="seller", is_active=True, token_version=0)
            session.add_all([user, seller])
            await session.commit()
            self.owner_id, self.seller_id = user.id, seller.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_paid_arithmetic_boundary_pagination_and_csv_safety(self) -> None:
        from backend.models import Order
        from backend.services.fulfillment import ReportService

        base = datetime(2026, 1, 1, tzinfo=UTC)
        async with self.sf() as session:
            rows = []
            fixtures = (
                (" =2+2", 100, "confirmed", "paid", False, self.owner_id, base),
                (" +SUM(A1)", 110, "confirmed", "paid", False, self.owner_id, base + timedelta(hours=1)),
                (" -10", 120, "shipped", "paid", False, self.owner_id, base + timedelta(hours=2)),
                (" @cmd", 130, "confirmed", "paid", False, self.seller_id, base + timedelta(hours=3)),
                ("safe", 200, "shipped", "paid", False, self.owner_id, base + timedelta(hours=4)),
                ("unpaid", 400, "confirmed", "pending", False),
                ("cancelled", 500, "cancelled", "paid", False),
                ("expired", 600, "expired", "paid", False),
                ("recon", 700, "confirmed", "paid", True),
                ("at-end", 999, "confirmed", "paid", False, self.owner_id, base + timedelta(days=1)),
            )
            for index, fixture in enumerate(fixtures):
                number, amount, state, payment, reconciliation, *extra = fixture
                seller_id = extra[0] if extra else self.owner_id
                created_at = extra[1] if len(extra) > 1 else base
                rows.append(
                    Order(
                        public_id=number,
                        created_by_id=seller_id,
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
                        draft_token=f"token-{index}",
                        created_at=created_at,
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
            self.assertEqual((report["order_count"], report["revenue"]), (5, 660))
            self.assertEqual(len(report["items"]), 1)
            first_id = report["items"][0].id
            page = await service.revenue(start=base, end=base + timedelta(days=1), limit=1, cursor=report["next_cursor"])
            self.assertEqual(len(page["items"]), 1)
            self.assertEqual((page["order_count"], page["revenue"]), (5, 660))
            self.assertGreater(page["items"][0].id, first_id)
            status = await service.revenue(start=base, end=base + timedelta(days=1), status="confirmed", limit=100)
            self.assertEqual((status["order_count"], status["revenue"]), (3, 340))
            seller = await service.revenue(start=base, end=base + timedelta(days=1), seller_id=self.seller_id, limit=100)
            self.assertEqual((seller["order_count"], seller["revenue"]), (1, 130))
            self.assertEqual(seller["items"][0].created_by_id, self.seller_id)
            all_rows = await service.revenue(start=base, end=base + timedelta(days=1), limit=100)
            self.assertEqual([row.public_id for row in all_rows["items"]], [" =2+2", " +SUM(A1)", " -10", " @cmd", "safe"])
            self.assertEqual(all_rows["items"][0].created_at, base)
            self.assertNotIn("at-end", [row.public_id for row in all_rows["items"]])
            self.assertIsNone((await service.revenue(start=base + timedelta(days=1), end=base + timedelta(days=2)))["next_cursor"])
            body = await service.csv(start=base, end=base + timedelta(days=1))
            for formula in ("=2+2", "+SUM(A1)", "-10", "@cmd"):
                self.assertIn("' " + formula, body)


if __name__ == "__main__":
    unittest.main()

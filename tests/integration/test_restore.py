from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


class RestoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Round-trip a synthetic database through pg_dump/pg_restore."""

    async def test_restore_preserves_ids_counts_and_constraints(self) -> None:
        raw = os.getenv("TEST_DATABASE_URL")
        if not raw:
            self.skipTest("TEST_DATABASE_URL must target the dedicated local portfolio test DB")
        source = make_url(raw)
        if source.host not in {"localhost", "127.0.0.1", "::1"} or source.database != "onlineshop_portfolio_test":
            raise RuntimeError("test_restore requires local onlineshop_portfolio_test only")
        target = source.set(database="onlineshop_restore_test")
        env = os.environ.copy()
        env["TEST_DATABASE_URL"] = source.render_as_string(hide_password=False)
        root = Path(__file__).parents[2]
        python = os.environ.get("PORTFOLIO_PYTHON", sys.executable)
        subprocess.run([python, "-m", "scripts.reset_synthetic"], cwd=root, env=env, check=True)
        subprocess.run([python, "-m", "scripts.seed_synthetic", "--reset"], cwd=root, env=env, check=True)
        source_engine = create_async_engine(source.render_as_string(hide_password=False), poolclass=NullPool)
        async with source_engine.connect() as conn:
            before = {
                table: (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                for table in ("users", "admins", "products", "inventory_balances")
            }
            ids = (await conn.execute(text("SELECT sku, id FROM products ORDER BY sku"))).all()
        await source_engine.dispose()
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "portfolio.dump"
            subprocess.run(
                ["powershell", "-NoProfile", "-File", str(root / "scripts" / "backup.ps1"),
                 "-DatabaseUrl", env["TEST_DATABASE_URL"], "-OutputPath", str(backup)],
                cwd=root, env=env, check=True
            )
            target_env = env.copy()
            target_env["TEST_DATABASE_URL"] = target.render_as_string(hide_password=False)
            subprocess.run(
                ["powershell", "-NoProfile", "-File", str(root / "scripts" / "restore.ps1"),
                 "-DatabaseUrl", target_env["TEST_DATABASE_URL"], "-BackupPath", str(backup)],
                cwd=root, env=target_env, check=True
            )
        target_engine = create_async_engine(target.render_as_string(hide_password=False), poolclass=NullPool)
        async with target_engine.connect() as conn:
            after = {table: (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() for table in before}
            restored_ids = (await conn.execute(text("SELECT sku, id FROM products ORDER BY sku"))).all()
            constraints = {row[0] for row in (await conn.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'products'::regclass"))).all()}
        await target_engine.dispose()
        self.assertEqual(after, before)
        self.assertEqual(restored_ids, ids)
        self.assertIn("products_pkey", constraints)
        self.assertIn("products_sku_key", constraints)
        self.assertIn("ck_products_price_nonnegative", constraints)


if __name__ == "__main__":
    unittest.main()

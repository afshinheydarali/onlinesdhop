"""Deterministic authentication input and limiter unit tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "auth-unit-test-secret-32-characters")


class AuthSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_limiter_scopes_direct_ip_and_username_and_cleans_memory(self) -> None:
        from backend.auth import AuthRateLimiter

        now = [0.0]
        limiter = AuthRateLimiter(clock=lambda: now[0], window_seconds=10, max_failures=2, block_seconds=5, max_entries=6)
        self.assertIsNone(limiter.record_failure("10.0.0.1", "alice"))
        self.assertEqual(limiter.record_failure("10.0.0.1", "alice"), 5)
        self.assertEqual(limiter.check("10.0.0.2", "alice"), 5)
        self.assertIsNone(limiter.check("10.0.0.2", "bob"))
        self.assertEqual(len(limiter._buckets), 3)
        now[0] = 16.0
        self.assertIsNone(limiter.check("10.0.0.3", "bob"))
        self.assertEqual(len(limiter._buckets), 0)

    def test_password_limit_is_checked_before_hashing(self) -> None:
        from backend.auth import hash_password, verify_password

        with self.assertRaises(ValueError):
            hash_password("x" * 129)
        with self.assertRaises(ValueError):
            hash_password("é" * 65)
        self.assertFalse(verify_password("x" * 129, "not-a-hash"))

    async def test_bootstrap_parser_has_no_password_argument_and_reads_getpass(self) -> None:
        from backend import bootstrap

        args = bootstrap.build_parser().parse_args(["--username", "owner", "--telegram-id", "7"])
        self.assertEqual((args.username, args.telegram_id), ("owner", 7))
        with self.assertRaises(SystemExit):
            bootstrap.build_parser().parse_args(["--username", "owner", "--telegram-id", "7", "--password", "secret"])

        class Session:
            def __init__(self) -> None:
                self.added = None

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            def add(self, value: object) -> None:
                self.added = value

            async def commit(self) -> None:
                return None

        session = Session()
        with patch.object(bootstrap, "SessionFactory", return_value=session), patch.object(bootstrap.getpass, "getpass", return_value="secret"):
            await bootstrap.main(["--username", "owner", "--telegram-id", "7"])
        self.assertIsNotNone(session.added)
        self.assertTrue(session.added.password_hash)


if __name__ == "__main__":
    unittest.main()

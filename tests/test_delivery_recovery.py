import tempfile
import unittest
from pathlib import Path

from order_bot.database import Database
from tests.test_database import draft


class DeliveryRecoveryDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "orders.sqlite3"))
        self.db.initialize()
        self.db.add_admin(100, "Seller", "ADM-1")
        self.db.add_admin(101, "Other", "ADM-2")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_recovery_survives_new_database_and_is_owner_scoped(self) -> None:
        saved = self.db.save_order(100, draft("durable"), allow_duplicate=True).order
        assert saved is not None
        self.db.mark_delivery_failed(saved["id"], "TelegramNetworkError")
        reopened = Database(self.db.path)
        rows = reopened.list_recoverable_orders(100)
        self.assertEqual([row["public_id"] for row in rows], [saved["public_id"]])
        self.assertEqual(reopened.list_recoverable_orders(101), [])

    def test_recovery_is_bounded_and_ordered(self) -> None:
        for token in ("first", "second", "third"):
            self.db.save_order(100, draft(token), allow_duplicate=True)
        rows = self.db.list_recoverable_orders(100, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["draft_token"], "first")
        self.assertEqual(self.db.list_recoverable_orders(100, limit=2, offset=2)[0]["draft_token"], "third")

    def test_interrupted_delivery_is_explicitly_ambiguous(self) -> None:
        saved = self.db.save_order(100, draft("interrupted"), allow_duplicate=True).order
        assert saved is not None
        self.assertTrue(self.db.claim_delivery(saved["id"]))
        self.db.recover_interrupted_deliveries()
        recovered = self.db.get_order_by_id(saved["id"])
        assert recovered is not None
        self.assertEqual(recovered["delivery_status"], "failed")
        self.assertIn("Ambiguous", recovered["delivery_error"])


if __name__ == "__main__":
    unittest.main()

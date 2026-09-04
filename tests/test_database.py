import tempfile
import unittest
from pathlib import Path

from order_bot.database import Database


def draft(token: str, phone: str = "989121234567") -> dict[str, object]:
    return {
        "draft_token": token,
        "customer_name": "مشتری تست",
        "phone": "09121234567",
        "phone_normalized": phone,
        "province": "تهران",
        "city": "تهران",
        "address": "آدرس تست",
        "postal_code": None,
        "product": "SKU-1",
        "product_normalized": "sku-1",
        "quantity": 1,
        "amount": None,
        "notes": None,
        "photo_file_id": "file-1",
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "orders.sqlite3"))
        self.db.initialize()
        self.db.add_admin(100, "فروشنده", "ADM-001")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_admin_access_and_deactivation(self) -> None:
        self.assertIsNotNone(self.db.get_admin(100))
        self.assertIsNone(self.db.get_admin(999))
        self.assertTrue(self.db.set_admin_active(100, False))
        self.assertIsNone(self.db.get_admin(100))

    def test_admin_identifiers_are_unique(self) -> None:
        with self.assertRaises(ValueError):
            self.db.add_admin(100, "دیگری", "ADM-002")
        with self.assertRaises(ValueError):
            self.db.add_admin(101, "دیگری", "adm-001")

    def test_duplicate_requires_second_confirmation_without_disclosure(self) -> None:
        first = self.db.save_order(100, draft("one"), allow_duplicate=False)
        self.assertTrue(first.created)
        second = self.db.save_order(100, draft("two"), allow_duplicate=False)
        self.assertTrue(second.duplicate_confirmation_required)
        self.assertIsNone(second.order)
        confirmed = self.db.save_order(100, draft("two"), allow_duplicate=True)
        self.assertEqual(confirmed.order["duplicate_of"], first.order["id"])

    def test_repeated_confirmation_is_idempotent(self) -> None:
        first = self.db.save_order(100, draft("same"), allow_duplicate=False)
        second = self.db.save_order(100, draft("same"), allow_duplicate=True)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.order["id"], second.order["id"])

    def test_delivery_failure_can_be_claimed_for_retry(self) -> None:
        order = self.db.save_order(100, draft("retry"), allow_duplicate=False).order
        self.assertTrue(self.db.claim_delivery(order["id"]))
        self.assertFalse(self.db.claim_delivery(order["id"]))
        self.db.mark_delivery_failed(order["id"], "network")
        self.assertTrue(self.db.claim_delivery(order["id"]))


if __name__ == "__main__":
    unittest.main()


import unittest

from order_bot.validation import normalize_phone, normalize_product, parse_caption


class ValidationTests(unittest.TestCase):
    def test_phone_variants_and_persian_digits(self) -> None:
        expected = "989121234567"
        for raw in ("09121234567", "+989121234567", "989121234567", "۰۹۱۲-۱۲۳-۴۵۶۷"):
            self.assertEqual(normalize_phone(raw), expected)

    def test_phone_decimal_scripts_are_canonical_ascii(self) -> None:
        self.assertEqual(normalize_phone("०९१२१२३४५६७"), "989121234567")

    def test_invalid_phone(self) -> None:
        with self.assertRaises(ValueError):
            normalize_phone("123")

    def test_product_normalization(self) -> None:
        self.assertEqual(normalize_product("  AbC   ۱۲  "), "abc ۱۲")

    def test_valid_caption(self) -> None:
        parsed = parse_caption(
            "نام: علی رضایی\nتلفن: ۰۹۱۲۱۲۳۴۵۶۷\nاستان: تهران\nشهر: تهران\n"
            "آدرس: خیابان نمونه\nکدپستی:\nمحصول: SKU-1\nتعداد: ۲\nمبلغ: ۱۰۰٬۰۰۰\nتوضیحات: تست"
        )
        self.assertEqual(parsed["phone_normalized"], "989121234567")
        self.assertEqual(parsed["quantity"], 2)
        self.assertEqual(parsed["amount"], 100000)

    def test_invalid_caption_falls_back(self) -> None:
        with self.assertRaisesRegex(ValueError, "caption"):
            parse_caption("متن آزاد")


if __name__ == "__main__":
    unittest.main()


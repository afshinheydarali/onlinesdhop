import os
import unittest
from unittest.mock import patch

from order_bot.config import Config


class ConfigTests(unittest.TestCase):
    def test_required_environment_and_types(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "Missing"):
            Config.from_env()

    def test_valid_environment(self) -> None:
        values = {
            "BOT_TOKEN": "test-token",
            "OWNER_TELEGRAM_ID": "123",
            "ORDERS_CHANNEL_ID": "-100123",
            "DUPLICATE_WINDOW_DAYS": "45",
            "APP_TIMEZONE": "Asia/Tehran",
        }
        with patch.dict(os.environ, values, clear=True):
            config = Config.from_env()
        self.assertEqual(config.owner_telegram_id, 123)
        self.assertEqual(config.duplicate_window_days, 45)


if __name__ == "__main__":
    unittest.main()

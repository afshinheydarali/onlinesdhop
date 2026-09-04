import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from order_bot.config import Config


class ConfigTests(unittest.TestCase):
    def test_required_environment_and_types(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "Missing"):
            Config.from_env(None)

    def test_valid_environment(self) -> None:
        values = {
            "BOT_TOKEN": "test-token",
            "OWNER_TELEGRAM_ID": "123",
            "ORDERS_CHANNEL_ID": "-100123",
            "DUPLICATE_WINDOW_DAYS": "45",
            "APP_TIMEZONE": "Asia/Tehran",
        }
        with patch.dict(os.environ, values, clear=True):
            config = Config.from_env(None)
        self.assertEqual(config.owner_telegram_id, 123)
        self.assertEqual(config.duplicate_window_days, 45)

    def test_env_file_is_loaded_without_overriding_process_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "BOT_TOKEN=file-token\nOWNER_TELEGRAM_ID=123\nORDERS_CHANNEL_ID=-100123\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"BOT_TOKEN": "process-token"}, clear=True):
                config = Config.from_env(str(env_file))
        self.assertEqual(config.bot_token, "process-token")
        self.assertEqual(config.owner_telegram_id, 123)


if __name__ == "__main__":
    unittest.main()

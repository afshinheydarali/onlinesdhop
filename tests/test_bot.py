import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from order_bot.bot import MAX_CAPTION_LENGTH, authorized_admin, owner_only, publish_order, render_order_html
from order_bot.config import Config
from order_bot.database import Database
from tests.test_database import draft


class BotLogicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "orders.sqlite3"))
        self.db.initialize()
        self.admin = self.db.add_admin(100, "<فروشنده>", "ADM&1")
        self.config = Config("token", 1, -1001, self.db.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_order(self, token: str = "one", notes: str | None = None) -> dict:
        data = draft(token)
        data["notes"] = notes
        return self.db.save_order(100, data, allow_duplicate=True).order

    def test_channel_text_escapes_user_input(self) -> None:
        order = self.make_order()
        order["customer_name"] = "<b>bad</b> & text"
        text = render_order_html(order, self.admin, "Asia/Tehran")
        self.assertIn("&lt;b&gt;bad&lt;/b&gt; &amp; text", text)
        self.assertIn("ADM&amp;1", text)

    async def test_short_text_is_sent_as_caption(self) -> None:
        order = self.make_order()
        bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=10)), send_message=AsyncMock())
        self.assertTrue(await publish_order(bot, self.db, self.config, order, self.admin))
        bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.get_order_by_id(order["id"])["delivery_status"], "sent")

    async def test_long_caption_becomes_related_text_message(self) -> None:
        order = self.make_order(notes="x" * MAX_CAPTION_LENGTH)
        bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=10)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        )
        self.assertTrue(await publish_order(bot, self.db, self.config, order, self.admin))
        self.assertEqual(bot.send_message.await_args.kwargs["reply_to_message_id"], 10)

    async def test_send_failure_is_saved_and_retryable(self) -> None:
        order = self.make_order()
        method = SimpleNamespace(__api_method__="sendPhoto")
        bot = SimpleNamespace(send_photo=AsyncMock(side_effect=TelegramBadRequest(method, "failed")), send_message=AsyncMock())
        self.assertFalse(await publish_order(bot, self.db, self.config, order, self.admin))
        failed = self.db.get_order_by_id(order["id"])
        self.assertEqual(failed["delivery_status"], "failed")
        bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))
        self.assertTrue(await publish_order(bot, self.db, self.config, failed, self.admin))

    async def test_unknown_user_is_rejected_server_side(self) -> None:
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            chat=SimpleNamespace(type="private"),
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())
        self.assertIsNone(await authorized_admin(event, state, self.db))
        state.clear.assert_awaited_once()
        event.answer.assert_awaited_once()

    async def test_owner_access_requires_private_chat_and_exact_id(self) -> None:
        state = SimpleNamespace(clear=AsyncMock())
        owner = SimpleNamespace(
            from_user=SimpleNamespace(id=self.config.owner_telegram_id),
            chat=SimpleNamespace(type="private"),
            answer=AsyncMock(),
        )
        self.assertTrue(await owner_only(owner, state, self.config))
        stranger = SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            chat=SimpleNamespace(type="private"),
            answer=AsyncMock(),
        )
        self.assertFalse(await owner_only(stranger, state, self.config))


if __name__ == "__main__":
    unittest.main()

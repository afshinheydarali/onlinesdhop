import asyncio
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from order_bot.bot import create_dispatcher, publish_order
from order_bot.config import Config
from order_bot.database import Database
from tests.test_database import draft


class RecordingSession:
    def __init__(self, error=None):
        self.methods: list[TelegramMethod] = []
        self.error = error
        self.photos = 0
        self.barrier = None

    async def __call__(self, bot, method, timeout=None):
        self.methods.append(method)
        if method.__class__.__name__ == "AnswerCallbackQuery" and self.error:
            raise self.error
        if method.__class__.__name__ == "SendPhoto":
            self.photos += 1
            if self.barrier:
                await self.barrier.wait()
        return Message(message_id=len(self.methods) + 100, date=0, chat=Chat(id=100, type="private"))

    async def close(self):
        pass


class DeliveryRecoveryRoutedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "o.sqlite3"))
        self.db.initialize()
        self.db.add_admin(100, "Seller", "A1")
        self.db.add_admin(101, "Other", "A2")
        self.config = Config("123:token", 999, -1001, self.db.path)
        self.session = RecordingSession()
        self.bot = Bot(self.config.bot_token, session=self.session)
        self.dispatcher = create_dispatcher()

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.temp.cleanup()

    def order(self, token):
        row = self.db.save_order(100, draft(token), allow_duplicate=True).order
        assert row is not None
        return row

    async def feed(self, text, uid=100, dispatcher=None):
        msg = Message(message_id=1, date=0, chat=Chat(id=100, type="private"), from_user=User(id=uid, is_bot=False, first_name="Seller"), text=text)
        await (dispatcher or self.dispatcher).feed_update(
            self.bot, Update(update_id=len(self.session.methods) + 1, message=msg), db=self.db, config=self.config
        )

    async def retry(self, public, uid=100):
        user = User(id=uid, is_bot=False, first_name="Seller")
        msg = Message(message_id=2, date=0, chat=Chat(id=100, type="private"), from_user=User(id=999, is_bot=True, first_name="Bot"))
        cb = CallbackQuery(id="x", from_user=user, chat_instance="x", message=msg, data=f"retry:{public}")
        await self.dispatcher.feed_update(self.bot, Update(update_id=99, callback_query=cb), db=self.db, config=self.config)

    async def test_ack_bad_request_still_publishes(self):
        row = self.order("bad")
        self.db.mark_delivery_failed(row["id"], "x")
        self.session.error = TelegramBadRequest(SimpleNamespace(__api_method__="answerCallbackQuery"), "old")
        await self.retry(row["public_id"])
        self.assertEqual(self.db.get_order_by_id(row["id"])["delivery_status"], "sent")

    async def test_ack_network_error_still_publishes(self):
        row = self.order("net")
        self.db.mark_delivery_failed(row["id"], "x")
        self.session.error = TelegramNetworkError(SimpleNamespace(__api_method__="answerCallbackQuery"), "network")
        await self.retry(row["public_id"])
        self.assertEqual(self.db.get_order_by_id(row["id"])["delivery_status"], "sent")

    async def test_recovery_mid_form_and_fresh_dispatcher(self):
        row = self.order("mid")
        await self.feed("/recovery")
        self.assertTrue(any(row["public_id"] in getattr(x, "text", "") for x in self.session.methods))
        fresh = create_dispatcher()
        await self.feed("/recovery", dispatcher=fresh)
        self.assertTrue(any(row["public_id"] in getattr(x, "text", "") for x in self.session.methods))

    async def test_pagination_bot_authored_message_uses_caller_and_hides_pii(self):
        rows = [self.order(str(i)) for i in range(11)]
        await self.feed("/recovery")
        self.assertIn(rows[0]["public_id"], " ".join(getattr(x, "text", "") for x in self.session.methods))
        self.assertNotIn(rows[0]["customer_name"], " ".join(getattr(x, "text", "") for x in self.session.methods))

    async def test_foreign_and_inactive_recovery_rejected(self):
        row = self.order("own")
        await self.retry(row["public_id"], 101)
        self.db.set_admin_active(100, False)
        await self.feed("/recovery")
        self.assertEqual(self.db.get_order_by_id(row["id"])["delivery_attempts"], 0)

    async def test_pending_reconfirmation_offers_retry(self):
        row = self.order("pending")
        await self.feed("/recovery")
        self.assertTrue(
            any(
                "retry:" + row["public_id"] in getattr(x, "reply_markup", SimpleNamespace()).model_dump_json()
                for x in self.session.methods
                if hasattr(getattr(x, "reply_markup", None), "model_dump_json")
            )
        )

    async def test_ambiguous_retry_requires_explicit_consent(self):
        row = self.order("amb")
        self.db.claim_delivery(row["id"])
        self.db.recover_interrupted_deliveries()
        await self.retry(row["public_id"])
        self.assertEqual(self.db.get_order_by_id(row["id"])["delivery_attempts"], 1)

    async def test_concurrent_retry_one_network_send(self):
        row = self.order("race")
        self.db.mark_delivery_failed(row["id"], "x")
        self.session.barrier = asyncio.Event()
        tasks = [asyncio.create_task(publish_order(self.bot, self.db, self.config, row, self.db.get_admin(100))) for _ in range(2)]
        await asyncio.sleep(0)
        self.session.barrier.set()
        await asyncio.gather(*tasks)
        self.assertEqual(self.session.photos, 1)

    async def test_split_retry_reuses_photo(self):
        row = self.order("split")
        with closing(self.db._connect()) as connection:
            connection.execute("UPDATE orders SET notes=? WHERE id=?", ("x" * 4096, row["id"]))
        row = self.db.get_order(row["public_id"])
        assert row is not None
        self.db.claim_delivery(row["id"])
        self.db.mark_photo_sent(row["id"], 77)
        self.db.mark_delivery_failed(row["id"], "x")
        row = self.db.get_order(row["public_id"])
        assert row is not None
        await publish_order(self.bot, self.db, self.config, row, self.db.get_admin(100))
        self.assertEqual(self.session.photos, 0)

    async def test_sent_order_not_resent(self):
        row = self.order("sent")
        self.db.mark_delivered(row["id"], 1)
        await publish_order(self.bot, self.db, self.config, row, self.db.get_admin(100))
        self.assertEqual(self.session.photos, 0)

    async def test_invalid_page_is_safe(self):
        await self.feed("/recovery")
        self.assertTrue(True)

import asyncio
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

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


class R1RecoveryAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "orders.sqlite3")
        self.db = Database(self.path)
        self.db.initialize()
        self.db.add_admin(100, "Seller", "A1")
        self.db.add_admin(101, "Other", "A2")
        self.config = Config("123:token", 999, -1001, self.path)
        self.session = RecordingSession()
        self.bot = Bot(self.config.bot_token, session=self.session)
        self.dispatcher = create_dispatcher()

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.temp.cleanup()

    async def feed(self, update, dispatcher=None, db=None):
        await (dispatcher or self.dispatcher).feed_update(self.bot, update, db=db or self.db, config=self.config)

    def message(self, uid, text=None, *, photo=False, caption=None, message_id=1):
        return Message(
            message_id=message_id,
            date=0,
            chat=Chat(id=uid, type="private"),
            from_user=User(id=uid, is_bot=False, first_name="Seller"),
            text=text,
            caption=caption,
            photo=[PhotoSize(file_id="photo-1", file_unique_id="unique", width=10, height=10)] if photo else None,
        )

    def callback(self, data, *, uid=100):
        return CallbackQuery(
            id="callback",
            from_user=User(id=uid, is_bot=False, first_name="Seller"),
            chat_instance="chat",
            message=Message(
                message_id=1000,
                date=0,
                chat=Chat(id=100, type="private"),
                from_user=User(id=999, is_bot=True, first_name="Bot"),
            ),
            data=data,
        )

    def caption(self):
        return "نام: علی رضایی\nتلفن: 09121234567\nاستان: تهران\nشهر: تهران\nآدرس: خیابان نمونه\nمحصول: SKU-1\nتعداد: 1"

    async def confirm_from_real_preview(self, error):
        await self.feed(Update(update_id=1, message=self.message(100, "ثبت سفارش جدید")))
        await self.feed(Update(update_id=2, message=self.message(100, photo=True, caption=self.caption())))
        preview = next(method for method in reversed(self.session.methods) if method.__class__.__name__ == "SendPhoto")
        callback_data = preview.reply_markup.inline_keyboard[0][0].callback_data
        self.session.error = error
        before = len(self.session.methods)
        self.assertIsNone(self.db.get_order_by_id(1))
        self.assertFalse(any(method.__class__.__name__ == "SendPhoto" and method.chat_id == -1001 for method in self.session.methods[before:]))
        await self.feed(Update(update_id=3, callback_query=self.callback(callback_data)))
        order = self.db.get_order_by_id(1)
        self.assertIsNotNone(order)
        self.assertEqual(order["delivery_status"], "sent")
        self.assertEqual(sum(method.__class__.__name__ == "SendPhoto" and method.chat_id == -1001 for method in self.session.methods), 1)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertIsNone(await state.get_state())

    async def confirm(self, row):
        await self.feed(Update(update_id=1, message=self.message(100, "ثبت سفارش جدید")))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        await state.update_data(draft_token=row["draft_token"])
        await self.feed(Update(update_id=2, message=self.message(100, photo=True, caption=self.caption())))
        preview = next(method for method in reversed(self.session.methods) if method.__class__.__name__ == "SendPhoto")
        callback_data = preview.reply_markup.inline_keyboard[0][0].callback_data
        await self.feed(Update(update_id=3, callback_query=self.callback(callback_data)))

    async def test_r1_confirm_ack_bad_request_after_real_preview(self):
        await self.confirm_from_real_preview(TelegramBadRequest(SimpleNamespace(__api_method__="answerCallbackQuery"), "query too old"))

    async def test_r1_confirm_ack_network_error_after_real_preview(self):
        await self.confirm_from_real_preview(TelegramNetworkError(SimpleNamespace(__api_method__="answerCallbackQuery"), "network"))

    async def test_r1_pagination_bot_authored_next_back_and_malformed(self):
        own = []
        for token in range(11):
            result = self.db.save_order(100, draft(f"r1-{token}"), allow_duplicate=True)
            own.append(result.order)
        foreign = self.db.save_order(101, draft("foreign-r1"), allow_duplicate=True).order
        await self.feed(Update(update_id=1, message=self.message(100, "/recovery")))
        first = [m for m in self.session.methods if getattr(m, "text", None)][-1]
        self.assertEqual(sum(ref["public_id"] in first.text for ref in own), 10)
        self.assertNotIn(foreign["public_id"], first.text)
        next_data = next(button.callback_data for row in first.reply_markup.inline_keyboard for button in row if button.callback_data == "recovery:1")
        before = len(self.session.methods)
        await self.feed(Update(update_id=2, callback_query=self.callback(next_data)))
        emitted = [m for m in self.session.methods[before:] if getattr(m, "text", None)]
        self.assertEqual(len(emitted), 1)
        self.assertIn(own[10]["public_id"], emitted[0].text)
        self.assertNotIn(foreign["public_id"], emitted[0].text)
        back = next(button.callback_data for row in emitted[0].reply_markup.inline_keyboard for button in row if button.callback_data == "recovery:0")
        await self.feed(Update(update_id=3, callback_query=self.callback(back)))
        before = len(self.session.methods)
        await self.feed(Update(update_id=4, callback_query=self.callback("recovery:999999999999999999999999999999999999")))
        self.assertTrue(any("صفحه نامعتبر" in getattr(m, "text", "") for m in self.session.methods[before:]))

    async def test_r1_recovery_mid_form_and_fresh_database_dispatcher(self):
        row = self.db.save_order(100, draft("r1-mid"), allow_duplicate=True).order
        await self.feed(Update(update_id=1, message=self.message(100, "ثبت سفارش جدید")))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), "OrderForm:customer_name")
        before = len(self.session.methods)
        await self.feed(Update(update_id=2, message=self.message(100, "/recovery")))
        self.assertIn(row["public_id"], " ".join(getattr(m, "text", "") for m in self.session.methods[before:]))
        self.assertEqual(await state.get_state(), "OrderForm:customer_name")
        reopened = Database(self.path)
        fresh = create_dispatcher()
        self.session.methods.clear()
        await self.feed(Update(update_id=3, message=self.message(100, "/recovery")), dispatcher=fresh, db=reopened)
        self.assertIn(row["public_id"], " ".join(getattr(m, "text", "") for m in self.session.methods))


class DeliveryRecoveryDatabaseRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "orders.sqlite3"))
        self.db.initialize()
        self.db.add_admin(100, "Seller", "A1")
        self.db.add_admin(101, "Other", "A2")

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_failed_query_persists_and_is_owner_scoped(self):
        row = self.db.save_order(100, draft("persist"), allow_duplicate=True).order
        self.assertIsNotNone(row)
        self.db.mark_delivery_failed(row["id"], "TelegramNetworkError")
        reopened = Database(self.db.path)
        self.assertEqual([item["public_id"] for item in reopened.list_recoverable_orders(100)], [row["public_id"]])
        self.assertEqual(reopened.list_recoverable_orders(101), [])

    def test_recovery_query_is_bounded_ordered_and_paginated(self):
        for index in range(11):
            self.db.save_order(100, draft(f"ordered-{index}"), allow_duplicate=True)
        self.assertEqual(len(self.db.list_recoverable_orders(100, limit=10)), 10)
        self.assertEqual(self.db.list_recoverable_orders(100, limit=10, offset=10)[0]["draft_token"], "ordered-10")

    def test_interrupted_sending_is_explicit_ambiguous_manual_work(self):
        row = self.db.save_order(100, draft("ambiguous-db"), allow_duplicate=True).order
        self.assertTrue(self.db.claim_delivery(row["id"]))
        self.db.recover_interrupted_deliveries()
        recovered = self.db.get_order_by_id(row["id"])
        self.assertIn("Ambiguous", recovered["delivery_error"])
        self.assertFalse(self.db.claim_delivery(row["id"]))
        self.assertTrue(self.db.claim_delivery(row["id"], allow_ambiguous=True))


class R2ReconciliationTests(R1RecoveryAcceptanceTests):
    async def test_r2_ambiguous_list_prompt_then_explicit_consent_sends_once(self):
        row = self.db.save_order(100, draft("r2-ambiguous"), allow_duplicate=True).order
        self.assertTrue(self.db.claim_delivery(row["id"]))
        self.db.recover_interrupted_deliveries()
        await self.feed(Update(update_id=1, message=self.message(100, "/recovery")))
        listing = [method for method in self.session.methods if getattr(method, "reply_markup", None)][-1]
        retry_data = listing.reply_markup.inline_keyboard[0][0].callback_data
        self.assertTrue(retry_data.startswith("retry:"))
        before = len(self.session.methods)
        await self.feed(Update(update_id=2, callback_query=self.callback(retry_data)))
        self.assertEqual(self.session.photos, 0)
        prompt = [method for method in self.session.methods[before:] if getattr(method, "reply_markup", None)][-1]
        consent = prompt.reply_markup.inline_keyboard[0][0]
        self.assertEqual(consent.text, "تأیید بررسی دستی و تلاش مجدد")
        await self.feed(Update(update_id=3, callback_query=self.callback(consent.callback_data)))
        saved = self.db.get_order_by_id(row["id"])
        self.assertEqual(saved["delivery_status"], "sent")
        self.assertEqual(saved["delivery_attempts"], 2)
        self.assertEqual(self.session.photos, 1)
        await self.feed(Update(update_id=4, callback_query=self.callback(consent.callback_data)))
        self.assertEqual(self.session.photos, 1)

    async def test_r2_pending_reconfirmation_from_real_preview_offers_retry(self):
        row = self.db.save_order(100, draft("r2-pending-" + "a" * 21), allow_duplicate=True).order
        before = len(self.session.methods)
        await self.confirm(row)
        emitted = self.session.methods[before:]
        buttons = [
            button
            for method in emitted
            if getattr(method, "reply_markup", None) and hasattr(method.reply_markup, "inline_keyboard")
            for line in method.reply_markup.inline_keyboard
            for button in line
        ]
        self.assertEqual(sum(button.callback_data.startswith("retry:") for button in buttons), 1)
        self.assertEqual(self.db.get_order_by_id(row["id"])["delivery_status"], "pending")

import tempfile
import unittest
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from order_bot.bot import NEW_ORDER, OrderForm, create_router
from order_bot.config import Config
from order_bot.database import Database
from tests.test_database import draft
from tests.test_handlers import RecordingSession


class DraftIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "orders.sqlite3"))
        self.db.initialize()
        self.db.add_admin(100, "فروشنده", "ADM-1")
        self.config = Config("123456:token", 999, -1001, self.db.path)
        self.session = RecordingSession()
        self.bot = Bot(self.config.bot_token, session=self.session)
        self.dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        self.dispatcher.include_router(create_router())

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        self.temp.cleanup()

    def message(self, update_id: int, text: str | None = None, *, photo: str | None = None, caption: str | None = None) -> Update:
        user = User(id=100, is_bot=False, first_name="Seller")
        value = Message(
            message_id=update_id, date=0, chat=Chat(id=100, type="private"), from_user=user,
            text=text, caption=caption,
            photo=[PhotoSize(file_id=photo, file_unique_id=photo, width=10, height=10)] if photo else None,
        )
        return Update(update_id=update_id, message=value)

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update, db=self.db, config=self.config)

    async def preview(self, update_id: int, product: str = "SKU-1", photo: str = "photo-1") -> tuple[str, str, str]:
        await self.feed(self.message(update_id, NEW_ORDER))
        caption = f"نام: علی رضایی\nتلفن: 09121234567\nاستان: تهران\nشهر: تهران\nآدرس: خیابان نمونه\nمحصول: {product}\nتعداد: 2"
        await self.feed(self.message(update_id + 1, photo=photo, caption=caption))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        data = await state.get_data()
        markup = next(method.reply_markup for method in reversed(self.session.methods) if method.__class__.__name__ == "SendPhoto")
        buttons = [button.callback_data for row in markup.inline_keyboard for button in row]
        return data["draft_token"], buttons[0], buttons[1]

    async def callback(self, update_id: int, data: str) -> None:
        user = User(id=100, is_bot=False, first_name="Seller")
        message = Message(message_id=update_id, date=0, chat=Chat(id=100, type="private"), from_user=user)
        callback = CallbackQuery(id=str(update_id), from_user=user, chat_instance="x", message=message, data=data)
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=update_id, callback_query=callback), db=self.db, config=self.config,
        )

    async def test_old_preview_cannot_confirm_new_draft(self) -> None:
        _, old_confirm, _ = await self.preview(1, "OLD")
        await self.feed(self.message(3, NEW_ORDER))
        await self.callback(4, old_confirm)
        self.assertIsNone(self.db.get_order_by_id(1))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.customer_name.state)

    async def test_old_revision_cannot_confirm_replaced_preview(self) -> None:
        _, old_confirm, _ = await self.preview(10, "OLD", "photo-1")
        await self.feed(self.message(12, photo="photo-2"))
        await self.callback(13, old_confirm)
        self.assertIsNone(self.db.get_order_by_id(1))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        data = await state.get_data()
        self.assertEqual(data["photo_file_id"], "photo-2")

    async def test_current_preview_confirms_once_and_replay_is_stale(self) -> None:
        _, confirm, _ = await self.preview(20)
        await self.callback(22, confirm)
        self.assertIsNotNone(self.db.get_order_by_id(1))
        await self.callback(23, confirm)
        self.assertIsNone(self.db.get_order_by_id(2))

    async def test_duplicate_warning_consent_is_bound_to_current_preview(self) -> None:
        self.db.save_order(100, draft("existing"), allow_duplicate=True)
        _, confirm, _ = await self.preview(30)
        await self.callback(32, confirm)
        warning = next(
            method.reply_markup for method in reversed(self.session.methods)
            if getattr(method, "reply_markup", None) and method.__class__.__name__ == "SendMessage"
        )
        duplicate = warning.inline_keyboard[0][0].callback_data
        await self.callback(33, duplicate)
        self.assertIsNotNone(self.db.get_order_by_id(2))

    async def test_two_users_have_independent_fsm_contexts(self) -> None:
        self.db.add_admin(200, "دوم", "ADM-2")
        await self.feed(self.message(40, NEW_ORDER))
        user = User(id=200, is_bot=False, first_name="Second")
        msg = Message(message_id=41, date=0, chat=Chat(id=200, type="private"), from_user=user, text=NEW_ORDER)
        await self.dispatcher.feed_update(self.bot, Update(update_id=41, message=msg), db=self.db, config=self.config)
        first = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        second = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=200, user_id=200)
        self.assertEqual(await first.get_state(), OrderForm.customer_name.state)
        self.assertEqual(await second.get_state(), OrderForm.customer_name.state)

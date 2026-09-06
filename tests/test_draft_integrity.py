import asyncio
import tempfile
import unittest
from pathlib import Path

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from order_bot.bot import NEW_ORDER, OrderForm, create_dispatcher
from order_bot.config import Config
from order_bot.database import Database
from tests.test_database import draft
from tests.test_handlers import RecordingSession


class BlockingSession(RecordingSession):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    async def __call__(self, bot: Bot, method: object, timeout: int | None = None):
        if method.__class__.__name__ == "SendPhoto" and getattr(method, "chat_id", None) == -1001:
            self.started.set()
            await self.release.wait()
        return await super().__call__(bot, method, timeout)


class DraftIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "orders.sqlite3"))
        self.db.initialize()
        self.db.add_admin(100, "فروشنده", "ADM-1")
        self.config = Config("123456:token", 999, -1001, self.db.path)
        self.session = RecordingSession()
        self.bot = Bot(self.config.bot_token, session=self.session)
        self.dispatcher = create_dispatcher()

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
        await self.callback(5, "o:c:malformed")
        self.assertEqual(await state.get_state(), OrderForm.customer_name.state)

    async def test_confirm_during_edit_cannot_use_retained_keys(self) -> None:
        _, confirm, edit = await self.preview(6)
        await self.callback(8, edit)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.customer_name.state)
        self.assertIn("customer_name", await state.get_data())
        await self.callback(9, confirm)
        self.assertIsNone(self.db.get_order_by_id(1))

    async def test_old_revision_cannot_confirm_replaced_preview(self) -> None:
        _, old_confirm, _ = await self.preview(10, "OLD", "photo-1")
        await self.feed(self.message(12, photo="photo-2"))
        await self.callback(13, old_confirm)
        self.assertIsNone(self.db.get_order_by_id(1))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        data = await state.get_data()
        self.assertEqual(data["photo_file_id"], "photo-2")

        latest = next(method.reply_markup for method in reversed(self.session.methods) if method.__class__.__name__ == "SendPhoto")
        await self.callback(14, latest.inline_keyboard[0][0].callback_data)
        self.assertEqual(self.db.get_order_by_id(1)["photo_file_id"], "photo-2")

    async def test_old_edit_and_cancel_cannot_touch_new_preview(self) -> None:
        _, old_confirm, old_edit = await self.preview(15, "OLD")
        old_markup = next(method.reply_markup for method in reversed(self.session.methods) if method.__class__.__name__ == "SendPhoto")
        old_cancel = old_markup.inline_keyboard[1][1].callback_data
        await self.feed(self.message(17, NEW_ORDER))
        caption = "نام: علی رضایی\nتلفن: 09121234567\nاستان: تهران\nشهر: تهران\nآدرس: خیابان نمونه\nمحصول: NEW\nتعداد: 1"
        await self.feed(self.message(18, photo="photo-new", caption=caption))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        for update_id, action in ((19, old_confirm), (20, old_edit), (21, old_cancel)):
            await self.callback(update_id, action)
        self.assertEqual(await state.get_state(), OrderForm.preview.state)
        self.assertIsNone(self.db.get_order_by_id(1))

    async def test_tampered_derived_fields_cannot_be_submitted(self) -> None:
        _, confirm, _ = await self.preview(25)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        await state.update_data(phone_normalized="00000000000")
        await self.callback(27, confirm)
        self.assertIsNone(self.db.get_order_by_id(1))

    async def test_duplicate_consent_expires_after_edit(self) -> None:
        self.db.save_order(100, draft("existing"), allow_duplicate=True)
        _, confirm, edit = await self.preview(35)
        await self.callback(37, confirm)
        warning = next(
            method.reply_markup for method in reversed(self.session.methods)
            if method.__class__.__name__ == "SendMessage" and getattr(method, "reply_markup", None)
        )
        duplicate = warning.inline_keyboard[0][0].callback_data
        await self.callback(38, edit)
        await self.callback(39, duplicate)
        self.assertIsNone(self.db.get_order_by_id(2))

    async def test_isolation_serializes_same_user_and_allows_other_user(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        self.session = BlockingSession(started, release)
        self.bot.session = self.session
        _, confirm, _ = await self.preview(45)
        # The preview photo is local chat traffic; only publication is blocked.
        confirm_task = asyncio.create_task(self.callback(47, confirm))
        await started.wait()
        new_draft_task = asyncio.create_task(self.feed(self.message(48, NEW_ORDER)))
        second_confirm_task = asyncio.create_task(self.callback(49, confirm))
        other = User(id=200, is_bot=False, first_name="Other")
        self.db.add_admin(200, "Other", "ADM-2")
        other_message = Message(message_id=50, date=0, chat=Chat(id=200, type="private"), from_user=other, text=NEW_ORDER)
        other_task = asyncio.create_task(self.dispatcher.feed_update(self.bot, Update(update_id=50, message=other_message), db=self.db, config=self.config))
        await other_task
        self.assertFalse(new_draft_task.done())
        release.set()
        await asyncio.gather(confirm_task, new_draft_task, second_confirm_task)
        self.assertIsNotNone(self.db.get_order_by_id(1))
        self.assertIsNone(self.db.get_order_by_id(2))
        published = [method for method in self.session.methods if method.__class__.__name__ == "SendPhoto" and getattr(method, "chat_id", None) == -1001]
        self.assertEqual(len(published), 1)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.customer_name.state)

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

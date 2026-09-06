import tempfile
import unittest
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from order_bot.bot import NEW_ORDER, SKIP, OrderForm, create_router, preview_keyboard
from order_bot.config import Config
from order_bot.database import Database
from tests.test_database import draft


class RecordingSession:
    def __init__(self) -> None:
        self.methods: list[TelegramMethod] = []
        self.next_message_id = 100

    async def __call__(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.methods.append(method)
        self.next_message_id += 1
        return Message(message_id=self.next_message_id, date=0, chat=Chat(id=100, type="private"))

    async def close(self) -> None:
        pass


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "orders.sqlite3"))
        self.db.initialize()
        self.db.add_admin(100, "فروشنده", "ADM-1")
        self.config = Config("123456:token", 999, -1001, self.db.path)
        self.session = RecordingSession()
        self.bot = Bot(self.config.bot_token, session=self.session)
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.dispatcher.include_router(create_router())

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        self.temp.cleanup()

    @staticmethod
    def message(update_id: int, text: str | None = None, *, photo: bool = False, caption: str | None = None) -> Update:
        user = User(id=100, is_bot=False, first_name="Seller")
        value = Message(
            message_id=update_id, date=0, chat=Chat(id=100, type="private"), from_user=user, text=text, caption=caption,
            photo=[PhotoSize(file_id="photo-1", file_unique_id="photo-unique-1", width=10, height=10)] if photo else None,
        )
        return Update(update_id=update_id, message=value)

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update, db=self.db, config=self.config)

    async def test_caption_shortcut_is_routed_and_saved(self) -> None:
        await self.feed(self.message(1, NEW_ORDER))
        caption = "نام: علی رضایی\nتلفن: 09121234567\nاستان: تهران\nشهر: تهران\nآدرس: خیابان نمونه\nمحصول: SKU-1\nتعداد: 2"
        update = self.message(2, photo=True, caption=caption)
        await self.feed(update)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.preview.state)
        data = await state.get_data()
        self.assertEqual(data["phone_normalized"], "989121234567")
        self.assertEqual(data["quantity"], 2)
        preview_message = Message(
            message_id=3, date=0, chat=Chat(id=100, type="private"),
            from_user=User(id=100, is_bot=False, first_name="Seller"),
        )
        callback = CallbackQuery(
            id="caption-confirm", from_user=preview_message.from_user, chat_instance="x",
            message=preview_message, data=preview_keyboard(data["draft_token"], data["preview_revision"]).inline_keyboard[0][0].callback_data,
        )
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=3, callback_query=callback), db=self.db, config=self.config,
        )
        order = self.db.get_order_by_id(1)
        self.assertIsNotNone(order)
        self.assertEqual(order["customer_name"], "علی رضایی")
        self.assertEqual(order["phone_normalized"], "989121234567")
        self.assertEqual(order["province"], "تهران")
        self.assertEqual(order["product_raw"], "SKU-1")
        self.assertEqual(order["quantity"], 2)

    async def test_full_form_optional_skips_and_confirmation_persists(self) -> None:
        await self.feed(self.message(10, NEW_ORDER))
        for update_id, value in enumerate(("علی رضایی", "09121234567", "تهران", "تهران", "خیابان نمونه", SKIP, "SKU-1", "2", SKIP, SKIP), 11):
            await self.feed(self.message(update_id, value))
        await self.feed(self.message(30, photo=True))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.preview.state)
        preview_message = Message(message_id=31, date=0, chat=Chat(id=100, type="private"), from_user=User(id=100, is_bot=False, first_name="Seller"))
        data = await state.get_data()
        callback_data = preview_keyboard(data["draft_token"], data["preview_revision"]).inline_keyboard[0][0].callback_data
        callback = CallbackQuery(
            id="confirm", from_user=preview_message.from_user, chat_instance="x", message=preview_message, data=callback_data,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=31, callback_query=callback), db=self.db, config=self.config)
        order = self.db.get_order_by_id(1)
        self.assertIsNotNone(order)
        self.assertEqual(order["phone_normalized"], "989121234567")
        self.assertEqual(order["postal_code"], None)
        self.assertEqual(order["customer_name"], "علی رضایی")
        self.assertEqual(order["quantity"], 2)

    async def test_invalid_caption_falls_back_to_first_step(self) -> None:
        await self.feed(self.message(40, NEW_ORDER))
        await self.feed(self.message(41, photo=True, caption="not a caption"))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.customer_name.state)

    async def test_navigation_and_deactivation_clear_or_restart_form(self) -> None:
        await self.feed(self.message(50, NEW_ORDER))
        await self.feed(self.message(51, "علی رضایی"))
        await self.feed(self.message(52, "بازگشت"))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.customer_name.state)
        await self.feed(self.message(52, "لغو"))
        self.assertIsNone(await state.get_state())
        await self.feed(self.message(53, NEW_ORDER))
        first_token = (await state.get_data())["draft_token"]
        await self.feed(self.message(54, "شروع مجدد"))
        self.assertNotEqual((await state.get_data())["draft_token"], first_token)
        await self.feed(self.message(55, "لغو"))
        self.assertIsNone(await state.get_state())
        await self.feed(self.message(56, NEW_ORDER))
        self.db.set_admin_active(100, False)
        await self.feed(self.message(57, "علی رضایی"))
        self.assertIsNone(await state.get_state())

    async def test_owner_admin_command_is_routed(self) -> None:
        user = User(id=999, is_bot=False, first_name="Owner")
        message = Message(message_id=3, date=0, chat=Chat(id=999, type="private"), from_user=user, text="/admin_add 101 ADM-2 New Seller")
        await self.dispatcher.feed_update(self.bot, Update(update_id=3, message=message), db=self.db, config=self.config)
        self.assertIsNotNone(self.db.get_admin(101, active_only=False))
        command_message = message.model_copy(update={"message_id": 6, "text": "/admin_disable 101"})
        await self.dispatcher.feed_update(self.bot, Update(update_id=6, message=command_message), db=self.db, config=self.config)
        self.assertFalse(self.db.get_admin(101, active_only=False).is_active)
        command_message = message.model_copy(update={"message_id": 7, "text": "/admin_enable 101"})
        await self.dispatcher.feed_update(self.bot, Update(update_id=7, message=command_message), db=self.db, config=self.config)
        self.assertTrue(self.db.get_admin(101).is_active)

    async def test_inactive_text_and_confirm_are_blocked(self) -> None:
        await self.feed(self.message(60, NEW_ORDER))
        await self.feed(self.message(61, "علی رضایی"))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.db.set_admin_active(100, False)
        await self.feed(self.message(62, "09121234567"))
        self.assertIsNone(await state.get_state())
        callback_message = Message(
            message_id=63, date=0, chat=Chat(id=100, type="private"),
            from_user=User(id=100, is_bot=False, first_name="Seller"),
        )
        callback = CallbackQuery(
            id="inactive-confirm", from_user=callback_message.from_user, chat_instance="x",
            message=callback_message, data="order:confirm",
        )
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=63, callback_query=callback), db=self.db, config=self.config,
        )
        self.assertTrue(any(getattr(method, "text", "") == "دسترسی شما فعال نیست." for method in self.session.methods))
        self.assertIsNone(self.db.get_order_by_id(1))

    async def test_foreign_retry_callback_cannot_access_order(self) -> None:
        self.db.add_admin(101, "Other", "ADM-2")
        saved = self.db.save_order(100, draft("ownership"), allow_duplicate=True).order
        self.assertIsNotNone(saved)
        user = User(id=101, is_bot=False, first_name="Other")
        message = Message(message_id=8, date=0, chat=Chat(id=100, type="private"), from_user=user)
        callback = CallbackQuery(id="retry", from_user=user, chat_instance="x", message=message, data=f"retry:{saved['public_id']}")
        await self.dispatcher.feed_update(self.bot, Update(update_id=8, callback_query=callback), db=self.db, config=self.config)
        self.assertTrue(any(getattr(method, "text", "") == "سفارش قابل‌دسترسی نیست." for method in self.session.methods))
        self.assertFalse(any(method.__class__.__name__ == "SendPhoto" for method in self.session.methods))
        self.assertEqual(self.db.get_order(saved["public_id"])["delivery_attempts"], 0)

    async def test_empty_admin_list_has_fallback(self) -> None:
        empty_db = Database(str(Path(self.temp.name) / "empty.sqlite3"))
        empty_db.initialize()
        user = User(id=999, is_bot=False, first_name="Owner")
        message = Message(message_id=9, date=0, chat=Chat(id=999, type="private"), from_user=user, text="/admins")
        await self.dispatcher.feed_update(self.bot, Update(update_id=9, message=message), db=empty_db, config=self.config)
        self.assertTrue(any(getattr(method, "text", "") == "هیچ ادمینی ثبت نشده است." for method in self.session.methods))

    async def test_nonprivate_callback_is_rejected(self) -> None:
        user = User(id=100, is_bot=False, first_name="Seller")
        message = Message(message_id=4, date=0, chat=Chat(id=-100, type="group"), from_user=user)
        callback = CallbackQuery(id="cb", from_user=user, chat_instance="x", message=message, data="order:cancel")
        await self.dispatcher.feed_update(self.bot, Update(update_id=4, callback_query=callback), db=self.db, config=self.config)
        self.assertTrue(any(getattr(method, "text", "") == "دسترسی شما فعال نیست." for method in self.session.methods))

    async def test_admin_list_is_split_between_complete_rows(self) -> None:
        for index in range(2, 90):
            self.db.add_admin(1000 + index, f"Seller {index} " + "x" * 110, f"ADM-{index:03d}")
        user = User(id=999, is_bot=False, first_name="Owner")
        message = Message(message_id=5, date=0, chat=Chat(id=999, type="private"), from_user=user, text="/admins")
        await self.dispatcher.feed_update(self.bot, Update(update_id=5, message=message), db=self.db, config=self.config)
        outputs = [method.text for method in self.session.methods if getattr(method, "text", None)]
        self.assertGreater(len(outputs), 1)
        self.assertTrue(all(len(output) <= 4096 for output in outputs))
        self.assertEqual(sum(output.count("|") for output in outputs), 3 * len(self.db.list_admins()))


if __name__ == "__main__":
    unittest.main()

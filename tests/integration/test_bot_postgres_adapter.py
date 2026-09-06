"""Routed Telegram acceptance tests for the production PostgreSQL adapter."""

import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import Admin as PgAdmin
from backend.models import IdempotencyKey, Order, Outbox
from backend.models import User as PgUser
from order_bot.bot import NEW_ORDER, OrderForm, create_router
from order_bot.config import Config
from order_bot.database import Database
from order_bot.persistence import PostgresPersistence, SQLitePersistence


def guarded_url(value: str | None) -> str:
    if not value:
        raise unittest.SkipTest("TEST_DATABASE_URL must be set")
    parsed = make_url(value)
    if parsed.host not in {"localhost", "127.0.0.1", "::1"} or not (parsed.database or "").endswith("_test"):
        raise RuntimeError("refusing non-local *_test database")
    return value


class RecordingSession:
    def __init__(self) -> None:
        self.methods: list[TelegramMethod] = []

    async def __call__(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.methods.append(method)
        return Message(message_id=len(self.methods) + 100, date=0, chat=Chat(id=100, type="private"))

    async def close(self) -> None:
        return None


class PostgresBotAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(guarded_url(os.getenv("TEST_DATABASE_URL")), poolclass=NullPool)
        async with self.engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE outbox, idempotency_keys, orders, admins, users CASCADE"))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all([
                PgUser(id=1001, telegram_id=100, username="telegram-seller", password_hash="!", role="seller", is_active=True, token_version=0),
                PgAdmin(telegram_id=100, name="Seller", admin_code="SELLER", is_active=True, created_at=datetime.now(UTC)),
            ])
            await session.commit()
        self.persistence = PostgresPersistence(self.sessions)
        self.config = Config("123456:token", 999, -1001, database_url=os.getenv("TEST_DATABASE_URL"))
        self.transport = RecordingSession()
        self.bot = Bot(self.config.bot_token, session=self.transport)
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.dispatcher.include_router(create_router())

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        await self.engine.dispose()

    @staticmethod
    def update(update_id: int, *, text_value: str | None = None, caption: str | None = None, photo: bool = False) -> Update:
        user = User(id=100, is_bot=False, first_name="Seller")
        return Update(update_id=update_id, message=Message(
            message_id=update_id, date=0, chat=Chat(id=100, type="private"), from_user=user,
            text=text_value, caption=caption,
            photo=[PhotoSize(file_id="photo-1", file_unique_id="photo-unique", width=10, height=10)] if photo else None,
        ))

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update, db=self.persistence, config=self.config)

    async def confirm_caption(self, update_id: int = 1, action: str = "c") -> str:
        await self.feed(self.update(update_id, text_value=NEW_ORDER))
        caption = "نام: علی رضایی\nتلفن: 09121234567\nاستان: تهران\nشهر: تهران\nآدرس: خیابان نمونه\nمحصول: SKU-1\nتعداد: 2"
        await self.feed(self.update(update_id + 1, photo=True, caption=caption))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        data = await state.get_data()
        callback_user = User(id=100, is_bot=False, first_name="Seller")
        callback_message = Message(
            message_id=update_id + 2, date=0, chat=Chat(id=100, type="private"),
            from_user=callback_user,
        )
        callback = CallbackQuery(
            id="confirm", from_user=callback_user, chat_instance="x",
            message=callback_message,
            data=f"o:{action}:{data['draft_token']}:{data['preview_revision']}",
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=update_id + 2, callback_query=callback), db=self.persistence, config=self.config)
        return callback.data or ""

    async def test_routed_confirmation_is_queued_and_atomic(self) -> None:
        await self.confirm_caption()
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(IdempotencyKey)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(Outbox)), 1)
            order = await session.scalar(select(Order))
            self.assertEqual(order.amount, None)
            self.assertEqual(order.phone_normalized, "989121234567")
        self.assertFalse(any(method.__class__.__name__ == "SendPhoto" and getattr(method, "chat_id", None) == -1001 for method in self.transport.methods))
        self.assertTrue(any("صف ارسال" in getattr(method, "text", "") for method in self.transport.methods))
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertIsNone(await state.get_state())

    async def test_replay_and_inactive_identity_are_safe(self) -> None:
        callback_data = await self.confirm_caption()
        callback_user = User(id=100, is_bot=False, first_name="Seller")
        callback_message = Message(message_id=30, date=0, chat=Chat(id=100, type="private"), from_user=callback_user)
        replay = CallbackQuery(
            id="replay", from_user=callback_user, chat_instance="x",
            message=callback_message, data=callback_data,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=30, callback_query=replay), db=self.persistence, config=self.config)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(IdempotencyKey)), 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(Outbox)), 1)
        self.assertTrue(any(
            marker in getattr(method, "text", "")
            for method in self.transport.methods
            for marker in ("قبلاً پردازش", "منقضی")
        ))
        async with self.sessions() as session:
            await session.execute(text("UPDATE admins SET is_active = false WHERE telegram_id = 100"))
            await session.commit()
        await self.feed(self.update(20, text_value=NEW_ORDER))
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Order)), 1)

    async def test_duplicate_warning_and_explicit_consent_are_routed(self) -> None:
        await self.confirm_caption()
        await self.confirm_caption(10)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=100, user_id=100)
        self.assertEqual(await state.get_state(), OrderForm.duplicate_warning.state)
        data = await state.get_data()
        user = User(id=100, is_bot=False, first_name="Seller")
        message = Message(message_id=13, date=0, chat=Chat(id=100, type="private"), from_user=user)
        callback = CallbackQuery(
            id="duplicate", from_user=user, chat_instance="x", message=message,
            data=f"o:d:{data['draft_token']}:{data['preview_revision']}",
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=13, callback_query=callback), db=self.persistence, config=self.config)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Order)), 2)
            self.assertEqual(await session.scalar(select(func.count()).select_from(Outbox)), 2)

    async def test_fresh_recovery_is_owner_scoped_and_worker_owned(self) -> None:
        await self.confirm_caption()
        async with self.sessions() as session:
            session.add(PgUser(id=1002, telegram_id=101, username="telegram-other", password_hash="!", role="seller", is_active=True, token_version=0))
            session.add(PgAdmin(telegram_id=101, name="Other", admin_code="OTHER", is_active=True, created_at=datetime.now(UTC)))
            await session.commit()
        # A fresh dispatcher must read only the caller's durable queue.
        fresh = Dispatcher(storage=MemoryStorage())
        fresh.include_router(create_router())
        await fresh.feed_update(self.bot, self.update(40, text_value="/recovery"), db=self.persistence, config=self.config)
        self.assertTrue(any("ORD-" in getattr(method, "text", "") for method in self.transport.methods))
        self.assertFalse(any("Other" in getattr(method, "text", "") for method in self.transport.methods))
        async with self.sessions() as session:
            order = await session.scalar(select(Order))
            order.delivery_status = "ambiguous"
            order.delivery_error = "manual reconciliation"
            await session.commit()
        await fresh.feed_update(self.bot, self.update(41, text_value="/recovery"), db=self.persistence, config=self.config)
        self.assertTrue(any("نیازمند بررسی دستی" in getattr(method, "text", "") for method in self.transport.methods))
        self.assertFalse(await self.persistence.claim_delivery(1))
        self.assertFalse(any(method.__class__.__name__ == "SendPhoto" and getattr(method, "chat_id", None) == -1001 for method in self.transport.methods))

    async def test_admin_disable_is_atomic_and_enable_does_not_restore_user(self) -> None:
        await self.persistence.set_admin_active(100, False)
        async with self.sessions() as session:
            user = await session.scalar(select(PgUser).where(PgUser.telegram_id == 100))
            self.assertFalse(user.is_active)
            version = user.token_version
        await self.persistence.set_admin_active(100, True)
        async with self.sessions() as session:
            user = await session.scalar(select(PgUser).where(PgUser.telegram_id == 100))
            self.assertFalse(user.is_active)
            self.assertEqual(user.token_version, version)

    async def test_admin_add_creates_telegram_user_atomically_and_collision_is_clean(self) -> None:
        created = await self.persistence.add_admin(101, "New Seller", "NEWSELLER")
        self.assertEqual(created.telegram_id, 101)
        async with self.sessions() as session:
            user = await session.scalar(select(PgUser).where(PgUser.telegram_id == 101))
            self.assertIsNotNone(user)
            self.assertEqual(user.role, "seller")
        with self.assertRaises(ValueError):
            await self.persistence.add_admin(102, "Other", "SELLER")
        async with self.sessions() as session:
            self.assertIsNone(await session.scalar(select(PgUser).where(PgUser.telegram_id == 102)))

    async def test_admin_add_rejects_existing_inactive_user(self) -> None:
        async with self.sessions() as session:
            session.add(PgUser(id=1003, telegram_id=103, username="telegram-inactive", password_hash="!", role="seller", is_active=False, token_version=2))
            await session.commit()
        with self.assertRaises(ValueError):
            await self.persistence.add_admin(103, "Inactive", "INACTIVE")
        self.assertIsNone(await self.persistence.get_admin(103, active_only=False))


class SQLiteAdapterThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_are_offloaded_as_complete_operations(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        database = Database(os.path.join(temporary.name, "orders.sqlite3"))
        database.initialize()
        caller = threading.get_ident()
        observed: list[int] = []
        original = database.get_admin

        def instrumented(*args, **kwargs):
            observed.append(threading.get_ident())
            return original(*args, **kwargs)

        database.get_admin = instrumented  # type: ignore[method-assign]
        await SQLitePersistence(database).get_admin(1)
        self.assertTrue(observed)
        self.assertNotEqual(observed[0], caller)
        temporary.cleanup()


if __name__ == "__main__":
    unittest.main()

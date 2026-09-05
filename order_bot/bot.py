from __future__ import annotations

import asyncio
import html
import logging
import sys
import uuid
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .config import Config
from .database import Admin, Database
from .validation import CAPTION_TEMPLATE, clean_text, normalize_phone, normalize_product, parse_caption, parse_optional_amount, parse_positive_int

LOGGER = logging.getLogger(__name__)
NEW_ORDER = "ثبت سفارش جدید"
CANCEL = "لغو"
BACK = "بازگشت"
RESTART = "شروع مجدد"
SKIP = "رد کردن"
MAX_CAPTION_LENGTH = 1024
MAX_MESSAGE_LENGTH = 4096


class ChannelAccessError(RuntimeError):
    pass


class OrderForm(StatesGroup):
    customer_name = State()
    phone = State()
    province = State()
    city = State()
    address = State()
    postal_code = State()
    product = State()
    quantity = State()
    amount = State()
    notes = State()
    photo = State()
    preview = State()


STEPS: list[tuple[str, State, str, bool]] = [
    ("customer_name", OrderForm.customer_name, "نام و نام خانوادگی مشتری را وارد کنید.", False),
    ("phone", OrderForm.phone, "شماره تماس مشتری را وارد کنید.", False),
    ("province", OrderForm.province, "استان را وارد کنید.", False),
    ("city", OrderForm.city, "شهر را وارد کنید.", False),
    ("address", OrderForm.address, "آدرس کامل را وارد کنید.", False),
    ("postal_code", OrderForm.postal_code, "کد پستی را وارد کنید یا «رد کردن» را بزنید.", True),
    ("product", OrderForm.product, "نام یا کد محصول را وارد کنید.", False),
    ("quantity", OrderForm.quantity, "تعداد را وارد کنید.", False),
    ("amount", OrderForm.amount, "مبلغ را به ریال وارد کنید یا «رد کردن» را بزنید.", True),
    ("notes", OrderForm.notes, "توضیحات را وارد کنید یا «رد کردن» را بزنید.", True),
    ("photo_file_id", OrderForm.photo, "حداقل یک عکس محصول بفرستید.", False),
]


def navigation_keyboard(*, optional: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BACK), KeyboardButton(text=CANCEL)], [KeyboardButton(text=RESTART)]]
    if optional:
        rows.insert(0, [KeyboardButton(text=SKIP)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=NEW_ORDER)]], resize_keyboard=True)


def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="تأیید و ارسال", callback_data="order:confirm")],
            [InlineKeyboardButton(text="ویرایش", callback_data="order:edit"), InlineKeyboardButton(text="لغو", callback_data="order:cancel")],
        ]
    )


def retry_keyboard(public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="تلاش مجدد ارسال", callback_data=f"retry:{public_id}")]])


def _event_user_and_chat(event: Message | CallbackQuery) -> tuple[int | None, str | None]:
    if isinstance(event, CallbackQuery):
        return event.from_user.id, event.message.chat.type if event.message else None
    return event.from_user.id if event.from_user else None, event.chat.type


async def authorized_admin(event: Message | CallbackQuery, state: FSMContext, db: Database) -> Admin | None:
    user_id, chat_type = _event_user_and_chat(event)
    admin = db.get_admin(user_id) if user_id and chat_type == ChatType.PRIVATE else None
    if admin:
        return admin
    await state.clear()
    text = "دسترسی شما فعال نیست."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text, reply_markup=ReplyKeyboardRemove())
    return None


async def owner_only(message: Message, state: FSMContext, config: Config) -> bool:
    if message.chat.type == ChatType.PRIVATE and message.from_user and message.from_user.id == config.owner_telegram_id:
        return True
    await state.clear()
    await message.answer("این دستور فقط برای مدیر اصلی است.", reply_markup=ReplyKeyboardRemove())
    return False


def parse_step(key: str, value: str) -> dict[str, Any]:
    limits = {"customer_name": 120, "province": 80, "city": 80, "address": 600, "postal_code": 20, "product": 200, "notes": 1000}
    labels = {"customer_name": "نام", "province": "استان", "city": "شهر", "address": "آدرس", "postal_code": "کدپستی", "product": "محصول", "notes": "توضیحات"}
    if key == "phone":
        raw = clean_text(value, maximum=30, field="تلفن")
        return {"phone": raw, "phone_normalized": normalize_phone(raw)}
    if key == "product":
        raw = clean_text(value, maximum=limits[key], field=labels[key])
        return {"product": raw, "product_normalized": normalize_product(raw)}
    if key == "quantity":
        return {key: parse_positive_int(value, field="تعداد", maximum=100_000)}
    if key == "amount":
        return {key: parse_optional_amount(value)}
    return {key: clean_text(value, maximum=limits[key], field=labels[key])}


def draft_text(data: dict[str, Any]) -> str:
    amount = f"{data['amount']:,} ریال" if data.get("amount") is not None else "—"
    return (
        f"نام مشتری: {data['customer_name']}\n"
        f"تلفن: {data['phone']}\n"
        f"استان/شهر: {data['province']} / {data['city']}\n"
        f"آدرس: {data['address']}\n"
        f"کد پستی: {data.get('postal_code') or '—'}\n"
        f"محصول: {data['product']}\n"
        f"تعداد: {data['quantity']}\n"
        f"مبلغ: {amount}\n"
        f"توضیحات: {data.get('notes') or '—'}"
    )


def render_order_html(order: dict[str, Any], admin: Admin, timezone: str) -> str:
    def esc(value: object | None) -> str:
        return html.escape(str(value)) if value not in (None, "") else "—"

    local_time = datetime.fromisoformat(order["created_at"]).astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")
    amount = f"{order['amount']:,} ریال" if order.get("amount") is not None else "—"
    return (
        f"<b>سفارش {esc(order['public_id'])}</b>\n"
        f"زمان ثبت: {esc(local_time)}\n"
        f"ادمین: {esc(admin.name)} ({esc(admin.admin_code)})\n"
        f"نام مشتری: {esc(order['customer_name'])}\n"
        f"تلفن: {esc(order['phone_raw'])}\n"
        f"استان/شهر: {esc(order['province'])} / {esc(order['city'])}\n"
        f"آدرس: {esc(order['address'])}\n"
        f"کد پستی: {esc(order.get('postal_code'))}\n"
        f"محصول: {esc(order['product_raw'])}\n"
        f"تعداد: {esc(order['quantity'])}\n"
        f"مبلغ: {esc(amount)}\n"
        f"توضیحات: {esc(order.get('notes'))}\n"
        f"تکراری: {'بله' if order.get('duplicate_of') else 'خیر'}"
    )


async def publish_order(bot: Bot, db: Database, config: Config, order: dict[str, Any], admin: Admin) -> bool:
    if not db.claim_delivery(order["id"]):
        current = db.get_order_by_id(order["id"])
        return bool(current and current["delivery_status"] == "sent")
    text = render_order_html(order, admin, config.app_timezone)
    try:
        if len(text) <= MAX_CAPTION_LENGTH:
            photo_message = await bot.send_photo(
                config.orders_channel_id, order["photo_file_id"], caption=text, parse_mode=ParseMode.HTML
            )
            photo_message_id: int | None = photo_message.message_id
            text_message_id = None
        else:
            photo_message_id = cast(int | None, order.get("channel_photo_message_id"))
            if not photo_message_id:
                photo_message = await bot.send_photo(config.orders_channel_id, order["photo_file_id"])
                photo_message_id = photo_message.message_id
                db.mark_photo_sent(order["id"], photo_message_id)
            text_message = await bot.send_message(
                config.orders_channel_id, text, parse_mode=ParseMode.HTML, reply_to_message_id=photo_message_id
            )
            text_message_id = text_message.message_id
        assert photo_message_id is not None
        db.mark_delivered(order["id"], photo_message_id, text_message_id)
        return True
    except Exception as exc:
        LOGGER.error("Order %s delivery failed: %s", order["public_id"], type(exc).__name__)
        db.mark_delivery_failed(order["id"], type(exc).__name__)
        return False


async def show_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = draft_text(data)
    await state.set_state(OrderForm.preview)
    if len(text) <= MAX_CAPTION_LENGTH:
        await message.answer_photo(data["photo_file_id"], caption=text, reply_markup=preview_keyboard())
    else:
        photo = await message.answer_photo(data["photo_file_id"])
        await message.answer(text, reply_to_message_id=photo.message_id, reply_markup=preview_keyboard())


async def prompt_step(message: Message, state: FSMContext, index: int) -> None:
    _, target_state, prompt, optional = STEPS[index]
    await state.set_state(target_state)
    await message.answer(prompt, reply_markup=navigation_keyboard(optional=optional))


def create_router() -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, db: Database, config: Config) -> None:
        await state.clear()
        user_id = message.from_user.id if message.from_user else 0
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("ربات را فقط در چت خصوصی استفاده کنید.")
        elif admin := db.get_admin(user_id):
            await message.answer(f"سلام {admin.name}. برای ثبت سفارش دکمه زیر را بزنید.", reply_markup=main_keyboard())
        elif user_id == config.owner_telegram_id:
            await message.answer("پنل مدیر آماده است. برای راهنما /owner_help را بزنید.")
        else:
            await message.answer("دسترسی شما فعال نیست.")

    @router.message(Command("owner_help"))
    async def owner_help(message: Message, state: FSMContext, config: Config) -> None:
        if await owner_only(message, state, config):
            await message.answer(
                "/admin_add TELEGRAM_ID ADMIN_CODE NAME\n"
                "/admin_enable TELEGRAM_ID\n/admin_disable TELEGRAM_ID\n/admins"
            )

    @router.message(Command("admin_add"))
    async def admin_add(message: Message, state: FSMContext, db: Database, config: Config) -> None:
        if not await owner_only(message, state, config):
            return
        parts = (message.text or "").split(maxsplit=3)
        if len(parts) != 4:
            await message.answer("قالب: /admin_add TELEGRAM_ID ADMIN_CODE NAME")
            return
        try:
            admin = db.add_admin(int(parts[1]), parts[3], parts[2])
        except ValueError as exc:
            await message.answer(str(exc))
        else:
            await message.answer(f"ادمین {admin.name} با کد {admin.admin_code} افزوده شد.")

    async def change_admin(message: Message, state: FSMContext, db: Database, config: Config, active: bool) -> None:
        if not await owner_only(message, state, config):
            return
        parts = (message.text or "").split()
        try:
            telegram_id = int(parts[1]) if len(parts) == 2 else 0
        except ValueError:
            telegram_id = 0
        if telegram_id <= 0:
            await message.answer(f"قالب: /admin_{'enable' if active else 'disable'} TELEGRAM_ID")
        elif db.set_admin_active(telegram_id, active):
            await message.answer("وضعیت ادمین به‌روزرسانی شد.")
        else:
            await message.answer("ادمین پیدا نشد.")

    @router.message(Command("admin_enable"))
    async def admin_enable(message: Message, state: FSMContext, db: Database, config: Config) -> None:
        await change_admin(message, state, db, config, True)

    @router.message(Command("admin_disable"))
    async def admin_disable(message: Message, state: FSMContext, db: Database, config: Config) -> None:
        await change_admin(message, state, db, config, False)

    @router.message(Command("admins"))
    async def admins(message: Message, state: FSMContext, db: Database, config: Config) -> None:
        if not await owner_only(message, state, config):
            return
        rows = db.list_admins()
        lines = [f"{item.admin_code} | {item.name} | {item.telegram_id} | {'فعال' if item.is_active else 'غیرفعال'}" for item in rows]
        if not lines:
            await message.answer("هیچ ادمینی ثبت نشده است.")
            return
        chunk = ""
        for line in lines:
            candidate = f"{chunk}\n{line}" if chunk else line
            if chunk and len(candidate) > MAX_MESSAGE_LENGTH:
                await message.answer(chunk)
                chunk = line
            else:
                chunk = candidate
        await message.answer(chunk)

    @router.message(F.text == NEW_ORDER)
    async def new_order(message: Message, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(message, state, db):
            return
        await state.clear()
        await state.update_data(draft_token=uuid.uuid4().hex)
        await prompt_step(message, state, 0)

    @router.message(F.text == CANCEL)
    async def cancel(message: Message, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(message, state, db):
            return
        await state.clear()
        await message.answer("سفارش لغو شد.", reply_markup=main_keyboard())

    @router.message(F.text == RESTART)
    async def restart(message: Message, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(message, state, db):
            return
        await state.clear()
        await state.update_data(draft_token=uuid.uuid4().hex)
        await prompt_step(message, state, 0)

    @router.message(F.text == BACK)
    async def back(message: Message, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(message, state, db):
            return
        current = await state.get_state()
        if not current or not current.startswith(f"{OrderForm.__name__}:"):
            await message.answer("ابتدا «ثبت سفارش جدید» را انتخاب کنید.", reply_markup=main_keyboard())
            return
        index = next((i for i, (_, item_state, _, _) in enumerate(STEPS) if item_state.state == current), 0)
        await prompt_step(message, state, max(0, index - 1))

    @router.message(F.photo)
    async def photo(message: Message, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(message, state, db):
            return
        current = await state.get_state()
        if not current or not current.startswith(f"{OrderForm.__name__}:"):
            await message.answer("ابتدا «ثبت سفارش جدید» را انتخاب کنید.")
            return
        photos = message.photo
        if not photos:
            return
        await state.update_data(photo_file_id=photos[-1].file_id)
        if message.caption:
            try:
                parsed = parse_caption(message.caption)
            except ValueError:
                if current != OrderForm.photo.state:
                    await message.answer("caption قابل‌پردازش نبود؛ اطلاعات را مرحله‌به‌مرحله وارد کنید.\n\nقالب نمونه:\n" + CAPTION_TEMPLATE)
                    await prompt_step(message, state, 0)
                    return
                await message.answer("caption نادیده گرفته شد؛ اطلاعات مرحله‌ای قبلی حفظ شد.")
            else:
                await state.update_data(cast(dict[str, Any], parsed))
                await show_preview(message, state)
                return
        data = await state.get_data()
        if current == OrderForm.photo.state and all(key in data for key, *_ in STEPS[:-1]):
            await show_preview(message, state)
        else:
            await message.answer("عکس دریافت شد؛ حالا اطلاعات مرحله جاری را وارد کنید.")

    async def collect_text(message: Message, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(message, state, db):
            return
        current = await state.get_state()
        index = next((i for i, (_, item_state, _, _) in enumerate(STEPS[:-1]) if item_state.state == current), None)
        if index is None or not message.text:
            await message.answer("ورودی نامعتبر است.")
            return
        key, _, _, optional = STEPS[index]
        if message.text == SKIP and optional:
            parsed = {key: None}
        else:
            try:
                parsed = parse_step(key, message.text)
            except ValueError as exc:
                await message.answer(str(exc))
                return
        await state.update_data(**parsed)
        data = await state.get_data()
        if index + 1 == len(STEPS) - 1 and data.get("photo_file_id"):
            await show_preview(message, state)
        else:
            await prompt_step(message, state, index + 1)

    for _, step_state, _, _ in STEPS[:-1]:
        router.message(step_state)(collect_text)

    @router.callback_query(F.data == "order:edit")
    async def edit(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(callback, state, db) or not isinstance(callback.message, Message):
            return
        if "draft_token" not in await state.get_data():
            await state.update_data(draft_token=uuid.uuid4().hex)
        await callback.answer()
        await prompt_step(callback.message, state, 0)

    @router.callback_query(F.data == "order:cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
        if not await authorized_admin(callback, state, db):
            return
        await state.clear()
        await callback.answer("لغو شد.")
        if callback.message:
            await callback.message.answer("سفارش لغو شد.", reply_markup=main_keyboard())

    async def confirm(callback: CallbackQuery, state: FSMContext, db: Database, config: Config, allow_duplicate: bool) -> None:
        admin = await authorized_admin(callback, state, db)
        if not admin or not isinstance(callback.message, Message):
            return
        data = await state.get_data()
        required = {"draft_token", *(key for key, *_ in STEPS)}
        if not required.issubset(data):
            await callback.answer("پیش‌نویس کامل نیست؛ دوباره شروع کنید.", show_alert=True)
            return
        try:
            result = db.save_order(admin.telegram_id, data, allow_duplicate=allow_duplicate)
        except PermissionError:
            await state.clear()
            await callback.answer("دسترسی شما فعال نیست.", show_alert=True)
            return
        if result.duplicate_confirmation_required:
            await callback.answer()
            await callback.message.answer(
                "سفارش مشابهی در بازه اخیر وجود دارد. بدون نمایش اطلاعات آن، آیا ثبت سفارش جدید را تأیید می‌کنید؟",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="ثبت با وجود تکراری بودن", callback_data="order:confirm_duplicate")],
                    [InlineKeyboardButton(text="لغو", callback_data="order:cancel")],
                ]),
            )
            return
        order = result.order
        if order is None:
            return
        if not result.created:
            current = db.get_order_by_id(order["id"])
            await state.clear()
            await callback.answer("این سفارش قبلاً پردازش شده است.", show_alert=True)
            if current and current["delivery_status"] == "failed":
                await callback.message.answer(
                    f"سفارش {order['public_id']} ذخیره شده ولی ارسال آن ناموفق بوده است.",
                    reply_markup=retry_keyboard(order["public_id"]),
                )
            return
        await callback.answer("در حال ارسال…")
        sent = await publish_order(cast(Bot, callback.bot), db, config, order, admin)
        await state.clear()
        if sent:
            await callback.message.answer(f"سفارش با شماره {order['public_id']} ثبت و ارسال شد.", reply_markup=main_keyboard())
        else:
            await callback.message.answer(
                f"سفارش {order['public_id']} ذخیره شد، اما ارسال ناموفق بود.",
                reply_markup=retry_keyboard(order["public_id"]),
            )

    @router.callback_query(F.data == "order:confirm")
    async def confirm_once(callback: CallbackQuery, state: FSMContext, db: Database, config: Config) -> None:
        await confirm(callback, state, db, config, False)

    @router.callback_query(F.data == "order:confirm_duplicate")
    async def confirm_duplicate(callback: CallbackQuery, state: FSMContext, db: Database, config: Config) -> None:
        await confirm(callback, state, db, config, True)

    @router.callback_query(F.data.startswith("retry:"))
    async def retry(callback: CallbackQuery, state: FSMContext, db: Database, config: Config) -> None:
        admin = await authorized_admin(callback, state, db)
        if not admin or not callback.data or not callback.message:
            return
        order = db.get_order(callback.data.removeprefix("retry:"), admin_id=admin.telegram_id)
        if not order:
            await callback.answer("سفارش قابل‌دسترسی نیست.", show_alert=True)
            return
        sent = await publish_order(cast(Bot, callback.bot), db, config, order, admin)
        await callback.answer("ارسال شد." if sent else "ارسال دوباره ناموفق بود.", show_alert=True)
        if sent:
            await callback.message.answer(f"سفارش {order['public_id']} ارسال شد.", reply_markup=main_keyboard())

    return router


async def validate_channel(bot: Bot, config: Config) -> None:
    me = await bot.get_me()
    try:
        chat = await bot.get_chat(config.orders_channel_id)
        administrators = await bot.get_chat_administrators(config.orders_channel_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        channel_format = "valid (-100...)" if str(config.orders_channel_id).startswith("-100") else "invalid"
        raise ChannelAccessError(
            "Telegram channel access check failed.\n\n"
            "Diagnostics:\n"
            f"- BOT_TOKEN is valid for @{me.username}.\n"
            f"- ORDERS_CHANNEL_ID format is {channel_format}.\n"
            f"- Telegram returned: {exc.message}\n\n"
            "Fix:\n"
            "1. Open the private management channel.\n"
            "2. Go to Manage Channel -> Administrators -> Add Administrator.\n"
            f"3. Add @{me.username} and enable the Post Messages permission.\n"
            "4. Copy a channel post link. For https://t.me/c/1234567890/15, "
            "set ORDERS_CHANNEL_ID=-1001234567890.\n"
            "5. Restart the bot.\n\n"
            "If the bot is already an administrator, verify that ORDERS_CHANNEL_ID belongs to that channel."
        ) from exc
    if chat.type != ChatType.CHANNEL:
        raise RuntimeError("ORDERS_CHANNEL_ID must refer to a channel")
    admin_ids = {member.user.id for member in administrators}
    if config.owner_telegram_id not in admin_ids or me.id not in admin_ids:
        raise RuntimeError("The owner and bot must both be channel administrators")
    unexpected = admin_ids - {config.owner_telegram_id, me.id}
    if unexpected:
        raise RuntimeError("The management channel has unexpected administrators")


async def run() -> None:
    config = Config.from_env()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(config.database_path, config.duplicate_window_days)
    db.initialize()
    recovered = db.recover_interrupted_deliveries()
    if recovered:
        LOGGER.warning("Marked %d interrupted deliveries for manual retry", recovered)
    bot = Bot(config.bot_token)
    try:
        await validate_channel(bot, config)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router())
        await dispatcher.start_polling(bot, db=db, config=config, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except ChannelAccessError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.models import Admin as PgAdmin
from backend.models import Order as PgOrder
from backend.models import User
from backend.services.orders import Actor, CreateOrderCommand, OrderService

from .database import Admin, Database, SaveResult


class AsyncPersistence(Protocol):
    postgres: bool

    async def initialize(self) -> None: ...
    async def recover_interrupted_deliveries(self) -> int: ...
    async def get_admin(self, telegram_id: int, *, active_only: bool = True) -> Admin | None: ...
    async def list_admins(self) -> list[Admin]: ...
    async def add_admin(self, telegram_id: int, name: str, admin_code: str) -> Admin: ...
    async def set_admin_active(self, telegram_id: int, active: bool) -> bool: ...
    async def save_order(self, admin_id: int, draft: Mapping[str, Any], *, allow_duplicate: bool) -> SaveResult: ...
    async def get_order(self, public_id: str, *, admin_id: int | None = None) -> dict[str, Any] | None: ...
    async def get_order_by_id(self, order_id: int) -> dict[str, Any] | None: ...
    async def list_recoverable_orders(self, admin_id: int, *, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]: ...
    async def claim_delivery(self, order_id: int, *, allow_ambiguous: bool = False) -> bool: ...
    async def mark_photo_sent(self, order_id: int, photo_message_id: int) -> None: ...
    async def mark_delivered(self, order_id: int, photo_message_id: int, text_message_id: int | None = None) -> None: ...
    async def mark_delivery_failed(self, order_id: int, error: str) -> None: ...


class SQLitePersistence:
    postgres = False

    def __init__(self, database: Database):
        self.database = database

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(getattr(self.database, name), *args, **kwargs)

    async def initialize(self) -> None:
        await self._call("initialize")

    async def recover_interrupted_deliveries(self) -> int:
        return await self._call("recover_interrupted_deliveries")

    async def get_admin(self, telegram_id: int, *, active_only: bool = True) -> Admin | None:
        return await self._call("get_admin", telegram_id, active_only=active_only)

    async def list_admins(self) -> list[Admin]:
        return await self._call("list_admins")

    async def add_admin(self, telegram_id: int, name: str, admin_code: str) -> Admin:
        return await self._call("add_admin", telegram_id, name, admin_code)

    async def set_admin_active(self, telegram_id: int, active: bool) -> bool:
        return await self._call("set_admin_active", telegram_id, active)

    async def save_order(self, admin_id: int, draft: Mapping[str, Any], *, allow_duplicate: bool) -> SaveResult:
        return await self._call("save_order", admin_id, draft, allow_duplicate=allow_duplicate)

    async def get_order(self, public_id: str, *, admin_id: int | None = None) -> dict[str, Any] | None:
        return await self._call("get_order", public_id, admin_id=admin_id)

    async def get_order_by_id(self, order_id: int) -> dict[str, Any] | None:
        return await self._call("get_order_by_id", order_id)

    async def list_recoverable_orders(self, admin_id: int, *, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        return await self._call("list_recoverable_orders", admin_id, limit=limit, offset=offset)

    async def claim_delivery(self, order_id: int, *, allow_ambiguous: bool = False) -> bool:
        return await self._call("claim_delivery", order_id, allow_ambiguous=allow_ambiguous)

    async def mark_photo_sent(self, order_id: int, photo_message_id: int) -> None:
        await self._call("mark_photo_sent", order_id, photo_message_id)

    async def mark_delivered(self, order_id: int, photo_message_id: int, text_message_id: int | None = None) -> None:
        await self._call("mark_delivered", order_id, photo_message_id, text_message_id)

    async def mark_delivery_failed(self, order_id: int, error: str) -> None:
        await self._call("mark_delivery_failed", order_id, error)


def _order_dict(order: PgOrder) -> dict[str, Any]:
    return {
        "id": order.id, "public_id": order.public_id, "admin_telegram_id": order.admin_telegram_id,
        "customer_name": order.customer_name, "phone_raw": order.phone_raw,
        "phone_normalized": order.phone_normalized, "province": order.province, "city": order.city,
        "address": order.address, "postal_code": order.postal_code, "product_raw": order.product_raw,
        "product_normalized": order.product_normalized, "quantity": order.quantity, "amount": order.amount,
        "notes": order.notes, "photo_file_id": order.photo_file_id, "duplicate_of": order.duplicate_of,
        "draft_token": order.draft_token, "created_at": order.created_at.isoformat(),
        "delivery_status": order.delivery_status, "delivery_attempts": order.delivery_attempts,
        "delivery_error": order.delivery_error, "channel_photo_message_id": order.channel_photo_message_id,
        "channel_text_message_id": order.channel_text_message_id, "delivered_at": order.delivered_at,
    }


class PostgresPersistence:
    postgres = True

    def __init__(self, sessions: async_sessionmaker, duplicate_window_days: int = 30):
        self.sessions = sessions
        self.duplicate_window_days = duplicate_window_days

    async def initialize(self) -> None:
        # Schema changes are explicit Alembic deployment steps.
        return None

    async def recover_interrupted_deliveries(self) -> int:
        return 0

    async def get_admin(self, telegram_id: int, *, active_only: bool = True) -> Admin | None:
        async with self.sessions() as session:
            query = select(PgAdmin).where(PgAdmin.telegram_id == telegram_id)
            if active_only:
                query = query.where(PgAdmin.is_active.is_(True))
            row = await session.scalar(query)
            return Admin(row.telegram_id, row.name, row.admin_code, row.is_active) if row else None

    async def list_admins(self) -> list[Admin]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(PgAdmin).order_by(PgAdmin.admin_code))).all()
            return [Admin(r.telegram_id, r.name, r.admin_code, r.is_active) for r in rows]

    async def add_admin(self, telegram_id: int, name: str, admin_code: str) -> Admin:
        # The owner boundary has already authenticated this operation. The
        # transaction makes the identity row visible atomically.
        async with self.sessions() as session:
            name = " ".join(name.strip().split())
            code = admin_code.strip().upper()
            if telegram_id <= 0 or not name or len(name) > 120 or not re.fullmatch(r"[A-Z0-9_-]{2,32}", code):
                raise ValueError("نام یا کد ادمین نامعتبر است.")
            existing_user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if existing_user is not None and not existing_user.is_active:
                raise ValueError("حساب کاربر غیرفعال است و بدون فعال‌سازی صریح دوباره قابل استفاده نیست.")
            if existing_user is None:
                # Telegram sellers have no password login; the unusable hash
                # keeps the API credential path closed while preserving one
                # durable internal identity for service attribution.
                session.add(
                    User(
                        username=f"telegram-{telegram_id}", telegram_id=telegram_id,
                        password_hash="!", role="seller", is_active=True, token_version=0,
                    )
                )
            row = PgAdmin(telegram_id=telegram_id, name=name, admin_code=code, created_at=datetime.now(UTC), is_active=True)
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("Telegram User ID یا کد ادمین قبلاً ثبت شده است.") from exc
            return Admin(row.telegram_id, row.name, row.admin_code, True)

    async def set_admin_active(self, telegram_id: int, active: bool) -> bool:
        async with self.sessions() as session:
            row = await session.get(PgAdmin, telegram_id)
            if row is None:
                return False
            row.is_active = active
            if not active:
                user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
                if user:
                    user.is_active = False
                    user.token_version += 1
            # Re-enabling an admin deliberately does not re-enable a user
            # independently revoked with the API.
            await session.commit()
            return True

    async def save_order(self, admin_id: int, draft: Mapping[str, Any], *, allow_duplicate: bool) -> SaveResult:
        async with self.sessions() as session:
            user = await session.scalar(select(User).where(User.telegram_id == admin_id))
            admin = await session.scalar(select(PgAdmin).where(PgAdmin.telegram_id == admin_id, PgAdmin.is_active.is_(True)))
            if user is None or admin is None:
                raise PermissionError("Admin is not active")
            command = CreateOrderCommand(
                draft["customer_name"], draft["phone"], draft["phone_normalized"], draft["province"], draft["city"],
                draft["address"], draft.get("postal_code"), draft["product"], draft["product_normalized"],
                draft["quantity"], draft.get("amount"), draft.get("notes"), draft["photo_file_id"],
                str(draft["draft_token"]), allow_duplicate,
            )
            result = await OrderService(session, self.duplicate_window_days).create_order(
                command, Actor(user.id, user.role, admin_id)
            )
            return SaveResult(_order_dict(result.order) if result.order else None, result.created, result.duplicate_confirmation_required)

    async def get_order(self, public_id: str, *, admin_id: int | None = None) -> dict[str, Any] | None:
        async with self.sessions() as session:
            query = select(PgOrder).where(PgOrder.public_id == public_id)
            if admin_id is not None:
                query = query.where(PgOrder.admin_telegram_id == admin_id)
            row = await session.scalar(query)
            return _order_dict(row) if row else None

    async def get_order_by_id(self, order_id: int) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(PgOrder, order_id)
            return _order_dict(row) if row else None

    async def list_recoverable_orders(self, admin_id: int, *, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            query = (
                select(PgOrder)
                .where(
                    PgOrder.admin_telegram_id == admin_id,
                    PgOrder.delivery_status.in_(("pending", "failed", "ambiguous")),
                )
                .order_by(PgOrder.created_at, PgOrder.id)
                .limit(min(max(limit, 1), 50))
                .offset(max(offset, 0))
            )
            rows = (await session.scalars(query)).all()
            return [_order_dict(r) for r in rows]

    async def claim_delivery(self, order_id: int, *, allow_ambiguous: bool = False) -> bool:
        return False  # PostgreSQL publication is owned by the outbox worker.

    async def mark_photo_sent(self, order_id: int, photo_message_id: int) -> None: return None
    async def mark_delivered(self, order_id: int, photo_message_id: int, text_message_id: int | None = None) -> None: return None
    async def mark_delivery_failed(self, order_id: int, error: str) -> None: return None

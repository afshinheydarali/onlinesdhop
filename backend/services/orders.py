from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Admin, IdempotencyKey, InventoryBalance, Order, OrderItem, Outbox, Product, Reservation, StockMovement, User
from order_bot.validation import clean_text, normalize_phone, normalize_product


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    user_id: int
    role: str
    telegram_id: int | None = None


@dataclass(frozen=True)
class CreateOrderCommand:
    customer_name: str
    phone_raw: str
    phone_normalized: str
    province: str
    city: str
    address: str
    postal_code: str | None
    product_raw: str
    product_normalized: str
    quantity: int
    amount: int | None
    notes: str | None
    photo_file_id: str
    idempotency_key: str
    allow_duplicate: bool = False


@dataclass(frozen=True)
class CreateOrderResult:
    order: Order | None
    created: bool
    duplicate_confirmation_required: bool = False


@dataclass(frozen=True)
class CartLine:
    sku: str
    quantity: int


@dataclass(frozen=True)
class CreateCartOrderCommand:
    customer_name: str
    phone_raw: str
    province: str
    city: str
    address: str
    postal_code: str | None
    items: tuple[CartLine, ...]
    idempotency_key: str
    notes: str | None = None


def _hash(command: CreateOrderCommand) -> str:
    value = asdict(command)
    value.pop("idempotency_key")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class OrderService:
    def __init__(self, session: AsyncSession, duplicate_window_days: int = 30):
        self.session = session
        self.duplicate_window_days = duplicate_window_days

    async def _active_actor(self, actor: Actor) -> User:
        user = await self.session.scalar(select(User).where(User.id == actor.user_id, User.is_active.is_(True)))
        if user is None or user.role != actor.role or user.telegram_id != actor.telegram_id:
            raise PermissionError("actor is not active")
        return user

    async def create_order(self, command: CreateOrderCommand, actor: Actor) -> CreateOrderResult:
        if actor.role not in {"owner", "manager", "seller"}:
            raise PermissionError("order creation is not permitted")
        if (
            type(command.quantity) is not int
            or command.quantity <= 0
            or command.quantity > 100_000
            or command.amount is not None
            and (type(command.amount) is not int or command.amount < 0 or command.amount > 10**15)
        ):
            raise ValueError("invalid quantity or amount")
        if not command.idempotency_key.strip() or len(command.idempotency_key) > 200:
            raise ValueError("idempotency key is required")
        if len(command.phone_raw) > 30 or len(command.photo_file_id) > 500:
            raise ValueError("input field too long")
        await self._active_actor(actor)
        if actor.telegram_id is not None:
            admin = await self.session.scalar(select(Admin).where(Admin.telegram_id == actor.telegram_id, Admin.is_active.is_(True)))
            if admin is None:
                raise PermissionError("actor has no active admin identity")
        # Canonicalize exactly the values that will be persisted before taking
        # the idempotency fingerprint. Equivalent accepted requests therefore
        # replay instead of conflicting merely because of whitespace.
        canonical = replace(
            command,
            customer_name=clean_text(command.customer_name, maximum=120, field="customer_name"),
            province=clean_text(command.province, maximum=80, field="province"),
            city=clean_text(command.city, maximum=80, field="city"),
            address=clean_text(command.address, maximum=600, field="address"),
            product_raw=clean_text(command.product_raw, maximum=200, field="product"),
            phone_raw=command.phone_raw.strip(),
            notes=clean_text(command.notes, maximum=1000, field="notes") if command.notes else None,
        )
        phone_normalized = normalize_phone(canonical.phone_raw)
        product_normalized = normalize_product(canonical.product_raw)
        if phone_normalized != command.phone_normalized or product_normalized != command.product_normalized:
            raise ValueError("normalized fields do not match raw values")
        canonical = replace(
            canonical,
            phone_normalized=phone_normalized,
            product_normalized=product_normalized,
        )
        # Lock idempotency scope first, then business duplicate scope, avoiding cross-pair races.
        scope_key = int.from_bytes(
            hashlib.blake2b(
                f"idem\0{actor.user_id}\0create_order\0{command.idempotency_key}".encode(),
                digest_size=8,
            ).digest(),
            "big",
            signed=True,
        )
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": scope_key})
        lock_key = int.from_bytes(
            hashlib.blake2b(f"{phone_normalized}\0{product_normalized}".encode(), digest_size=8).digest(),
            "big",
            signed=True,
        )
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        payload_hash = _hash(canonical)
        idem = await self.session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.actor_id == actor.user_id,
                IdempotencyKey.operation == "create_order",
                IdempotencyKey.key == command.idempotency_key,
            )
        )
        if idem:
            if idem.payload_hash != payload_hash:
                raise IdempotencyConflict("idempotency key payload conflict")
            existing = await self.session.get(Order, idem.order_id)
            if existing is None:
                raise RuntimeError("idempotency record has no order")
            await self.session.commit()
            return CreateOrderResult(existing, False)
        cutoff = datetime.now(UTC) - timedelta(days=self.duplicate_window_days)
        duplicate = await self.session.scalar(
            select(Order)
            .where(
                Order.phone_normalized == phone_normalized,
                Order.product_normalized == product_normalized,
                Order.created_at >= cutoff,
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        if duplicate and not command.allow_duplicate:
            # The duplicate itself is deliberately not returned to callers.
            await self.session.commit()
            return CreateOrderResult(None, False, True)
        now = datetime.now(UTC)
        order = Order(
            public_id=f"ORD-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            admin_telegram_id=actor.telegram_id,
            created_by_id=actor.user_id,
            customer_name=canonical.customer_name,
            phone_raw=canonical.phone_raw,
            phone_normalized=phone_normalized,
            province=canonical.province,
            city=canonical.city,
            address=canonical.address,
            postal_code=canonical.postal_code,
            product_raw=canonical.product_raw,
            product_normalized=product_normalized,
            quantity=canonical.quantity,
            amount=canonical.amount,
            notes=canonical.notes,
            photo_file_id=canonical.photo_file_id,
            duplicate_of=duplicate.id if duplicate else None,
            draft_token=uuid.uuid4().hex,
            created_at=now,
            delivery_status="pending",
            delivery_attempts=0,
        )
        self.session.add(order)
        await self.session.flush()
        self.session.add(
            IdempotencyKey(
                actor_id=actor.user_id,
                operation="create_order",
                key=command.idempotency_key,
                payload_hash=payload_hash,
                order_id=order.id,
            )
        )
        self.session.add(Outbox(order_id=order.id, status="pending", attempts=0, next_attempt_at=now))
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise
        return CreateOrderResult(order, True)

    async def create_cart_order(self, command: CreateCartOrderCommand, actor: Actor) -> CreateOrderResult:
        if actor.role not in {"owner", "manager", "seller"}:
            raise PermissionError("order creation is not permitted")
        await self._active_actor(actor)
        if not command.items or len(command.items) > 100:
            raise ValueError("at least one item is required")
        if any(type(x.quantity) is not int or x.quantity <= 0 for x in command.items):
            raise ValueError("item quantity must be positive")
        if len({x.sku for x in command.items}) != len(command.items):
            raise ValueError("duplicate SKU")
        if not command.idempotency_key.strip() or len(command.idempotency_key) > 200:
            raise ValueError("idempotency key is required")
        normalized_skus = tuple((x.sku.strip().upper(), x.quantity) for x in command.items)
        if any(not sku or len(sku) > 80 for sku, _ in normalized_skus):
            raise ValueError("invalid SKU")
        if len({sku for sku, _ in normalized_skus}) != len(normalized_skus):
            raise ValueError("duplicate SKU")
        canonical = {
            "customer_name": clean_text(command.customer_name, maximum=120, field="customer_name"),
            "phone_raw": command.phone_raw.strip(),
            "province": clean_text(command.province, maximum=80, field="province"),
            "city": clean_text(command.city, maximum=80, field="city"),
            "address": clean_text(command.address, maximum=600, field="address"),
            "postal_code": command.postal_code.strip() if command.postal_code else None,
            "items": sorted(normalized_skus),
            "notes": clean_text(command.notes, maximum=1000, field="notes") if command.notes else None,
        }
        payload_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        scope_key = int.from_bytes(
            hashlib.blake2b(f"idem\0{actor.user_id}\0create_cart_order\0{command.idempotency_key}".encode(), digest_size=8).digest(), "big", signed=True
        )
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": scope_key})
        idem = await self.session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.actor_id == actor.user_id, IdempotencyKey.operation == "create_cart_order", IdempotencyKey.key == command.idempotency_key
            )
        )
        if idem:
            if idem.payload_hash != payload_hash:
                raise IdempotencyConflict("idempotency key payload conflict")
            existing = await self.session.get(Order, idem.order_id)
            if existing is None:
                raise RuntimeError("idempotency record has no order")
            await self.session.commit()
            return CreateOrderResult(existing, False)
        skus = sorted(sku for sku, _ in normalized_skus)
        products = list(
            (
                await self.session.scalars(select(Product).where(Product.sku.in_(skus), Product.is_active.is_(True)).order_by(Product.sku).with_for_update())
            ).all()
        )
        if len(products) != len(skus):
            raise ValueError("unknown or inactive SKU")
        by_sku = {p.sku: p for p in products}
        balances = list(
            (
                await self.session.scalars(
                    select(InventoryBalance)
                    .where(InventoryBalance.product_id.in_([p.id for p in products]))
                    .order_by(InventoryBalance.product_id)
                    .with_for_update()
                )
            ).all()
        )
        by_product = {b.product_id: b for b in balances}
        lines = []
        total = 0
        for sku, quantity in normalized_skus:
            product, balance = by_sku[sku], by_product.get(by_sku[sku].id)
            if balance is None or balance.on_hand - balance.reserved < quantity:
                raise ValueError("insufficient stock")
            line_total = product.unit_price * quantity
            total += line_total
            lines.append((sku, quantity, product, balance, line_total))
        if total > 10**15:
            raise ValueError("order total is too large")
        now = datetime.now(UTC)
        phone = normalize_phone(canonical["phone_raw"])
        order = Order(
            public_id=f"ORD-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            admin_telegram_id=actor.telegram_id,
            created_by_id=actor.user_id,
            customer_name=canonical["customer_name"],
            phone_raw=canonical["phone_raw"],
            phone_normalized=phone,
            province=canonical["province"],
            city=canonical["city"],
            address=canonical["address"],
            postal_code=canonical["postal_code"],
            product_raw="[catalog]",
            product_normalized="[catalog]",
            quantity=sum(x[1] for x in normalized_skus),
            amount=total,
            currency="IRR",
            notes=canonical["notes"],
            photo_file_id="",
            draft_token=uuid.uuid4().hex,
            created_at=now,
            delivery_status="pending",
            delivery_attempts=0,
        )
        self.session.add(order)
        await self.session.flush()
        self.session.add(
            IdempotencyKey(actor_id=actor.user_id, operation="create_cart_order", key=command.idempotency_key, payload_hash=payload_hash, order_id=order.id)
        )
        for sku, quantity, product, balance, line_total in lines:
            balance.reserved += quantity
            self.session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    sku_snapshot=product.sku,
                    name_snapshot=product.name,
                    unit_price_snapshot=product.unit_price,
                    currency_snapshot=product.currency,
                    quantity=quantity,
                    line_total=line_total,
                )
            )
            self.session.add(Reservation(order_id=order.id, product_id=product.id, quantity=quantity, status="reserved"))
            self.session.add(StockMovement(product_id=product.id, order_id=order.id, quantity=quantity, movement_type="reserve"))
        self.session.add(Outbox(order_id=order.id, status="pending", attempts=0, next_attempt_at=now))
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return CreateOrderResult(order, True)

    async def get_order(self, public_id: str, actor: Actor) -> Order | None:
        if actor.role not in {"owner", "manager", "seller", "warehouse"}:
            raise PermissionError("unknown role")
        await self._active_actor(actor)
        query = select(Order).where(Order.public_id == public_id)
        if actor.role == "seller":
            query = query.where(Order.created_by_id == actor.user_id)
        return await self.session.scalar(query)

    async def list_orders(self, actor: Actor, *, limit: int = 50, cursor: int | None = None) -> list[Order]:
        if actor.role not in {"owner", "manager", "seller", "warehouse"}:
            raise PermissionError("unknown role")
        await self._active_actor(actor)
        limit = max(1, min(limit, 100))
        query = select(Order).order_by(Order.id.desc()).limit(limit)
        if actor.role == "seller":
            query = query.where(Order.created_by_id == actor.user_id)
        if cursor is not None:
            query = query.where(Order.id < cursor)
        return list((await self.session.scalars(query)).all())

    async def claim_delivery(self, order_id: int, *, worker_id: str, lease_seconds: int = 300):
        now = datetime.now(UTC)
        claim = uuid.uuid4().hex
        row = await self.session.scalar(select(Outbox).where(Outbox.order_id == order_id).with_for_update(skip_locked=True))
        if row is None or row.status in {"sent", "ambiguous", "failed"} or (row.lease_expires_at and row.lease_expires_at > now):
            await self.session.rollback()
            return None
        if row.status == "sending" and row.lease_expires_at and row.lease_expires_at <= now:
            row.status = "ambiguous"
            row.error_code = "lease_expired_manual_reconciliation"
            order = await self.session.get(Order, order_id)
            if order:
                order.delivery_status, order.delivery_error = (
                    "ambiguous",
                    row.error_code,
                )
            await self.session.commit()
            return None
        if row.next_attempt_at and row.next_attempt_at > now:
            await self.session.rollback()
            return None
        if row.attempts >= 5:
            row.status = "failed"
            row.error_code = "attempt_limit"
            await self.session.commit()
            return None
        row.status, row.worker_id, row.claim_token = "sending", worker_id, claim
        row.lease_expires_at, row.attempts = (
            now + timedelta(seconds=max(1, lease_seconds)),
            row.attempts + 1,
        )
        await self.session.commit()
        return row

    async def mark_delivery_sent(
        self,
        order_id: int,
        claim_token: str,
        photo_message_id: int,
        text_message_id: int | None = None,
    ) -> None:
        row = await self.session.scalar(select(Outbox).where(Outbox.order_id == order_id, Outbox.claim_token == claim_token).with_for_update())
        if row is None or row.status != "sending":
            raise ValueError("invalid delivery claim")
        row.status = "sent"
        order = await self.session.get(Order, order_id)
        if order:
            (
                order.delivery_status,
                order.channel_photo_message_id,
                order.channel_text_message_id,
                order.delivered_at,
                order.delivery_error,
            ) = "sent", photo_message_id, text_message_id, datetime.now(UTC), None
        await self.session.commit()

    async def mark_delivery_failed(
        self,
        order_id: int,
        claim_token: str,
        error_code: str,
        *,
        retry_at: datetime | None = None,
    ) -> None:
        row = await self.session.scalar(select(Outbox).where(Outbox.order_id == order_id, Outbox.claim_token == claim_token).with_for_update())
        if row is None or row.status != "sending":
            raise ValueError("invalid delivery claim")
        row.status, row.error_code, row.lease_expires_at, row.next_attempt_at = (
            ("pending" if retry_at else "failed"),
            error_code[:120],
            None,
            retry_at,
        )
        order = await self.session.get(Order, order_id)
        if order:
            order.delivery_status, order.delivery_error = row.status, error_code[:500]
        await self.session.commit()

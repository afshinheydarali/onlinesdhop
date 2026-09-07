from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)


class Admin(Base):
    __tablename__ = "admins"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    admin_code: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("uq_admins_code_ci", func.lower(admin_code), unique=True),)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(40), unique=True)
    admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("admins.telegram_id"), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    customer_name: Mapped[str] = mapped_column(Text)
    phone_raw: Mapped[str] = mapped_column(Text)
    phone_normalized: Mapped[str] = mapped_column(Text)
    province: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    product_raw: Mapped[str] = mapped_column(Text)
    product_normalized: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    notes: Mapped[str | None] = mapped_column(Text)
    photo_file_id: Mapped[str] = mapped_column(Text)
    duplicate_of: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id"))
    draft_token: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(String(20), default="pending")
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_error: Mapped[str | None] = mapped_column(String(500))
    channel_photo_message_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_text_message_id: Mapped[int | None] = mapped_column(BigInteger)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    payment_status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_orders_quantity_positive"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_orders_amount_nonnegative"),
        CheckConstraint(
            "delivery_status IN ('pending','sending','sent','failed','ambiguous')",
            name="ck_orders_delivery_status",
        ),
        CheckConstraint(
            "fulfillment_status IN ('draft','confirmed','packing','shipped','delivered','cancelled','expired')",
            name="ck_orders_fulfillment_status",
        ),
        CheckConstraint(
            "payment_status IN ('pending','paid','failed','refunded')",
            name="ck_orders_payment_status",
        ),
        Index(
            "ix_orders_duplicate",
            "phone_normalized",
            "product_normalized",
            "created_at",
        ),
        Index("ix_orders_fulfillment_status_created", "fulfillment_status", "created_at", "id"),
        Index("ix_orders_payment_report", "payment_status", "reconciliation_required", "created_at", "id"),
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    operation: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id"))
    __table_args__ = (UniqueConstraint("actor_id", "operation", "key", name="uq_idempotency_scope"),)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    worker_id: Mapped[str | None] = mapped_column(String(120))
    claim_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(120))


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    unit_price: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="IRR")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("unit_price >= 0", name="ck_products_price_nonnegative"),)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"))
    sku_snapshot: Mapped[str] = mapped_column(String(80))
    name_snapshot: Mapped[str] = mapped_column(String(200))
    unit_price_snapshot: Mapped[int] = mapped_column(BigInteger)
    currency_snapshot: Mapped[str] = mapped_column(String(3))
    quantity: Mapped[int] = mapped_column(Integer)
    line_total: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price_snapshot >= 0", name="ck_order_items_price_nonnegative"),
        CheckConstraint("line_total >= 0", name="ck_order_items_total_nonnegative"),
    )


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_inventory_on_hand_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_inventory_reserved_nonnegative"),
        CheckConstraint("reserved <= on_hand", name="ck_inventory_reserved_lte_on_hand"),
    )


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_reservations_quantity_positive"),
        CheckConstraint("status IN ('reserved','released','consumed','expired')", name="ck_reservations_status"),
        UniqueConstraint("order_id", "product_id", name="uq_reservation_order_product"),
    )


class FulfillmentTransition(Base):
    __tablename__ = "fulfillment_transitions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id", ondelete="CASCADE"))
    actor_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(20))
    from_status: Mapped[str] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    transitioned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_fulfillment_transitions_order_time", "order_id", "transitioned_at", "id"),)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(40), default="fake")
    provider_transaction_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20))
    payload_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_payment_provider_event"),
        UniqueConstraint("provider", "provider_transaction_id", name="uq_payment_provider_transaction"),
        Index("ix_payment_attempts_order", "order_id", "created_at"),
    )


class PaymentReconciliation(Base):
    """Durable manual follow-up for money that cannot be settled automatically."""

    __tablename__ = "payment_reconciliations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id", ondelete="CASCADE"))
    payment_attempt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payment_attempts.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("order_id", "kind", name="uq_payment_reconciliation_order_kind"),
        CheckConstraint("status IN ('open','resolved')", name="ck_payment_reconciliation_status"),
        Index("ix_payment_reconciliations_order", "order_id", "created_at", "id"),
    )


class PaymentEvent(Base):
    """Immutable signed provider callback receipt and replay fingerprint."""

    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), default="fake")
    provider_event_id: Mapped[str] = mapped_column(String(200))
    provider_transaction_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    payment_attempt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("payment_attempts.id", ondelete="SET NULL"), nullable=True
    )
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_payment_event_provider_event"),
        Index("ix_payment_events_provider_transaction", "provider", "provider_transaction_id"),
    )


# Compatibility name used by older adapters.
PaymentWebhookEvent = PaymentEvent


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"))
    order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer)
    movement_type: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_stock_movements_quantity_positive"),
        CheckConstraint("movement_type IN ('reserve','release','consume','adjust')", name="ck_stock_movements_type"),
        UniqueConstraint("order_id", "product_id", "movement_type", name="uq_stock_movement_order_product_type"),
    )

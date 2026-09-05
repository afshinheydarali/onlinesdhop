from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
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
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_orders_quantity_positive"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_orders_amount_nonnegative"),
        CheckConstraint("delivery_status IN ('pending','sending','sent','failed','ambiguous')", name="ck_orders_delivery_status"),
        Index("ix_orders_duplicate", "phone_normalized", "product_normalized", "created_at"),
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

"""Deterministic fake gateway verification and idempotent payment application.

The fake gateway is deliberately a wire protocol helper. It never performs a
network call and its callback verifier authenticates the exact bytes received
by the HTTP handler.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Order, PaymentAttempt, PaymentEvent


class PaymentError(ValueError):
    pass


class InvalidPaymentSignature(PaymentError):
    pass


class PaymentConflict(PaymentError):
    pass


@dataclass(frozen=True)
class PaymentResult:
    event_id: str
    order_id: int
    status: str
    applied: bool
    reconciliation_reason: str | None = None


def payment_hmac_secret() -> bytes:
    value = os.getenv("FAKE_PAYMENT_HMAC_SECRET", "")
    if not value:
        raise RuntimeError("FAKE_PAYMENT_HMAC_SECRET is not configured")
    return value.encode("utf-8")


class FakePaymentGateway:
    """The local fake's documented signature protocol."""

    @staticmethod
    def sign(raw_body: bytes, secret: str | bytes) -> str:
        key = secret.encode("utf-8") if isinstance(secret, str) else secret
        return "sha256=" + hmac.new(key, raw_body, hashlib.sha256).hexdigest()

    @staticmethod
    def payload(
        *, order_id: str, amount: int, currency: str = "IRR", provider_transaction_id: str,
        provider_event_id: str | None = None, status: str = "success",
    ) -> bytes:
        value = {
            "event_id": provider_event_id or uuid.uuid4().hex,
            "order_id": order_id,
            "amount": amount,
            "currency": currency,
            "provider_transaction_id": provider_transaction_id,
            "status": status,
        }
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# Short alias useful to adapters and external test fixtures.
FakeGateway = FakePaymentGateway


def _signature_bytes(value: str) -> bytes:
    value = value.strip()
    if value.startswith("sha256="):
        value = value[7:]
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise InvalidPaymentSignature("invalid payment signature") from exc
    if len(result) != hashlib.sha256().digest_size:
        raise InvalidPaymentSignature("invalid payment signature")
    return result


def _parse(raw_body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaymentError("invalid payment payload") from exc
    if not isinstance(value, dict):
        raise PaymentError("invalid payment payload")
    required = {"event_id", "order_id", "amount", "currency", "provider_transaction_id", "status"}
    aliases = {"event_id", "order_id", "amount", "currency", "transaction_id", "status"}
    named = {"provider_event_id", "order_id", "amount", "currency", "provider_transaction_id", "status"}
    if set(value) not in (required, aliases, named):
        raise PaymentError("invalid payment payload fields")
    if "provider_event_id" in value:
        value["event_id"] = value.pop("provider_event_id")
    if "provider_transaction_id" not in value:
        value["provider_transaction_id"] = value.pop("transaction_id")
    if any(not isinstance(value[k], str) or not value[k] for k in ("event_id", "order_id", "currency", "provider_transaction_id", "status")):
        raise PaymentError("invalid payment payload fields")
    if type(value["amount"]) is not int or value["amount"] < 0:
        raise PaymentError("invalid payment amount")
    if value["status"] not in {"success", "failed"}:
        raise PaymentError("invalid payment status")
    return value


class PaymentService:
    def __init__(self, session: AsyncSession, *, secret: bytes | str | None = None):
        self.session = session
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        self.secret = secret

    async def apply_callback(self, raw_body: bytes, signature: str) -> PaymentResult:
        secret = self.secret if self.secret is not None else payment_hmac_secret()
        try:
            provided = _signature_bytes(signature)
        except InvalidPaymentSignature:
            raise
        expected = hmac.new(secret, raw_body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, provided):
            raise InvalidPaymentSignature("invalid payment signature")
        payload = _parse(raw_body)
        event_id = payload["event_id"]
        fingerprint = hashlib.sha256(raw_body).hexdigest()

        # This lock only serializes callbacks for the same provider event. The
        # order row lock below serializes payment versus expiry/cancellation.
        event_key = int.from_bytes(hashlib.blake2b(event_id.encode(), digest_size=8).digest(), "big", signed=True)
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": event_key})
        tx_key = int.from_bytes(
            hashlib.blake2b(payload["provider_transaction_id"].encode(), digest_size=8).digest(),
            "big",
            signed=True,
        )
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": tx_key})
        existing = await self.session.scalar(select(PaymentEvent).where(PaymentEvent.provider_event_id == event_id))
        if existing is not None:
            if existing.payload_fingerprint != fingerprint:
                raise PaymentConflict("provider event replay has changed content")
            await self.session.commit()
            return PaymentResult(event_id, existing.order_id or 0, "duplicate", False)

        order = await self.session.scalar(
            select(Order).where(Order.public_id == payload["order_id"]).with_for_update()
        )
        if order is None:
            raise PaymentError("order not found")
        if order.amount is None:
            raise PaymentError("order amount is unknown")
        if payload["amount"] != order.amount:
            raise PaymentError("payment amount does not match order")
        expected_currency = (order.payment_currency or "IRR").upper()
        if payload["currency"].upper() != expected_currency:
            raise PaymentError("payment currency does not match order")

        transaction_id = payload["provider_transaction_id"]
        attempt = await self.session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.provider_transaction_id == transaction_id).with_for_update()
        )
        if attempt is not None:
            if attempt.order_id != order.id:
                raise PaymentConflict("provider transaction is bound to another order")
            self.session.add(PaymentEvent(
                provider_event_id=event_id, payload_fingerprint=fingerprint,
                provider_transaction_id=transaction_id, order_id=order.id,
                payment_attempt_id=attempt.id, received_at=datetime.now(UTC),
            ))
            await self.session.commit()
            return PaymentResult(event_id, order.id, "duplicate", False)

        now = datetime.now(UTC)
        late_reason: str | None = None
        if order.fulfillment_status in {"cancelled", "expired"}:
            late_reason = "order_" + order.fulfillment_status
        elif order.expires_at is not None and order.expires_at <= now:
            # Expiry wins while holding the same order lock used by the expiry
            # process. Keep the order's existing lifecycle state intact and
            # make the late-money decision visible to reconciliation.
            late_reason = "reservation_expired_before_payment"
            if order.fulfillment_status in {"draft", "confirmed"}:
                order.fulfillment_status = "expired"

        status = "reconciliation" if late_reason else ("paid" if payload["status"] == "success" else "failed")
        attempt = PaymentAttempt(
            order_id=order.id, provider="fake", provider_transaction_id=transaction_id,
            amount=payload["amount"], currency=expected_currency, status=status,
            reconciliation_reason=late_reason, created_at=now,
            paid_at=now if status == "paid" else None,
        )
        self.session.add(attempt)
        await self.session.flush()
        self.session.add(PaymentEvent(
            provider_event_id=event_id, payload_fingerprint=fingerprint,
            provider_transaction_id=transaction_id, order_id=order.id,
            payment_attempt_id=attempt.id, received_at=now,
        ))
        if late_reason:
            order.payment_status = "reconciliation"
        elif status == "paid":
            order.payment_status = "paid"
        else:
            order.payment_status = "failed"
        await self.session.commit()
        return PaymentResult(event_id, order.id, status, status == "paid", late_reason)


# Compatibility name for callers that model a webhook as an event.
PaymentWebhookService = PaymentService

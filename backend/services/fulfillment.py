from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    FulfillmentTransition,
    InventoryBalance,
    Order,
    PaymentAttempt,
    Reservation,
    StockMovement,
    User,
)
from backend.services.orders import Actor

FULFILLMENT_GRAPH: dict[str, frozenset[str]] = {
    "draft": frozenset({"confirmed", "cancelled", "expired"}),
    "confirmed": frozenset({"packing", "cancelled", "expired"}),
    "packing": frozenset({"shipped", "cancelled", "expired"}),
    "shipped": frozenset({"delivered"}),
    "delivered": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}
OPERATIONAL_ROLES = frozenset({"owner", "manager", "warehouse"})


class InvalidFulfillmentTransition(ValueError):
    pass


@dataclass(frozen=True)
class TransitionResult:
    order: Order
    changed: bool


def _now(value: datetime | None = None) -> datetime:
    return value or datetime.now(UTC)


class FulfillmentService:
    """Order lifecycle operations; payment and notification state stay independent."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _authorize(self, actor: Actor, *, allow_warehouse: bool = True) -> None:
        allowed = OPERATIONAL_ROLES if allow_warehouse else frozenset({"owner", "manager"})
        if actor.role not in allowed:
            raise PermissionError("fulfillment operation is not permitted")
        user = await self.session.scalar(select(User).where(User.id == actor.user_id, User.is_active.is_(True)))
        if user is None or user.role != actor.role or user.telegram_id != actor.telegram_id:
            raise PermissionError("actor is not active")

    async def _locked_order(self, public_id: str) -> Order:
        order = await self.session.scalar(select(Order).where(Order.public_id == public_id).with_for_update())
        if order is None:
            raise LookupError("order not found")
        return order

    async def _reservations(self, order_id: int) -> list[Reservation]:
        return list(
            (await self.session.scalars(select(Reservation).where(Reservation.order_id == order_id).order_by(Reservation.product_id).with_for_update())).all()
        )

    async def _release_locked(self, order: Order, *, status: str, at: datetime) -> None:
        reservations = await self._reservations(order.id)
        product_ids = sorted({r.product_id for r in reservations if r.status == "reserved"})
        balances = {
            b.product_id: b
            for b in (
                await self.session.scalars(
                    select(InventoryBalance).where(InventoryBalance.product_id.in_(product_ids)).order_by(InventoryBalance.product_id).with_for_update()
                )
            ).all()
        }
        for reservation in reservations:
            if reservation.status != "reserved":
                continue
            balance = balances.get(reservation.product_id)
            if balance is None or balance.reserved < reservation.quantity:
                raise RuntimeError("reservation invariant violated")
            balance.reserved -= reservation.quantity
            reservation.status = status
            self.session.add(
                StockMovement(
                    product_id=reservation.product_id,
                    order_id=order.id,
                    quantity=reservation.quantity,
                    movement_type="release",
                    created_at=at,
                )
            )

    async def transition(self, public_id: str, target: str, actor: Actor, reason: str, *, at: datetime | None = None) -> TransitionResult:
        await self._authorize(actor)
        if target not in FULFILLMENT_GRAPH:
            raise InvalidFulfillmentTransition("unknown fulfillment state")
        if not reason.strip() or len(reason) > 500:
            raise ValueError("reason is required")
        order = await self._locked_order(public_id)
        current = order.fulfillment_status
        if current == target:
            await self.session.commit()
            return TransitionResult(order, False)
        if target not in FULFILLMENT_GRAPH.get(current, frozenset()):
            raise InvalidFulfillmentTransition(f"cannot transition {current} to {target}")
        moment = _now(at)
        if target in {"cancelled", "expired"}:
            if actor.role == "warehouse" and target == "cancelled":
                raise PermissionError("warehouse cannot cancel orders")
            if actor.role == "warehouse" and target == "expired":
                raise PermissionError("warehouse cannot expire orders")
            if current == "shipped":
                raise InvalidFulfillmentTransition("shipped orders cannot be cancelled")
            if target == "expired":
                active = await self._reservations(order.id)
                if any(r.status == "reserved" and (r.expires_at is None or r.expires_at > moment) for r in active):
                    raise InvalidFulfillmentTransition("reservation has not expired")
            await self._release_locked(order, status="expired" if target == "expired" else "released", at=moment)
            if target == "expired":
                order.expired_at = moment
        if target == "shipped":
            await self._consume_locked(order, at=moment)
        order.fulfillment_status = target
        self.session.add(
            FulfillmentTransition(
                order_id=order.id,
                actor_id=actor.user_id,
                actor_role=actor.role,
                from_status=current,
                to_status=target,
                reason=reason.strip(),
                transitioned_at=moment,
            )
        )
        if target == "cancelled" and order.payment_status == "paid":
            order.reconciliation_required = True
        await self.session.commit()
        return TransitionResult(order, True)

    async def _consume_locked(self, order: Order, *, at: datetime) -> None:
        reservations = await self._reservations(order.id)
        product_ids = sorted({r.product_id for r in reservations if r.status == "reserved"})
        balances = {
            b.product_id: b
            for b in (
                await self.session.scalars(
                    select(InventoryBalance).where(InventoryBalance.product_id.in_(product_ids)).order_by(InventoryBalance.product_id).with_for_update()
                )
            ).all()
        }
        for reservation in reservations:
            if reservation.status != "reserved":
                continue
            balance = balances.get(reservation.product_id)
            if balance is None or balance.on_hand < reservation.quantity or balance.reserved < reservation.quantity:
                raise RuntimeError("insufficient reserved stock")
            balance.on_hand -= reservation.quantity
            balance.reserved -= reservation.quantity
            reservation.status = "consumed"
            self.session.add(
                StockMovement(
                    product_id=reservation.product_id,
                    order_id=order.id,
                    quantity=reservation.quantity,
                    movement_type="consume",
                    created_at=at,
                )
            )

    async def cancel_order(self, public_id: str, actor: Actor, reason: str = "customer cancellation") -> TransitionResult:
        return await self.transition(public_id, "cancelled", actor, reason)

    async def expire_order(self, public_id: str, actor: Actor, *, at: datetime | None = None, reason: str = "reservation expired") -> TransitionResult:
        await self._authorize(actor, allow_warehouse=False)
        if not reason.strip() or len(reason) > 500:
            raise ValueError("reason is required")
        moment = _now(at)
        order = await self.session.scalar(select(Order).where(Order.public_id == public_id).with_for_update())
        if order is None:
            raise LookupError("order not found")
        if order.fulfillment_status == "expired":
            await self.session.commit()
            return TransitionResult(order, False)
        if order.fulfillment_status in {"cancelled", "shipped", "delivered"}:
            raise InvalidFulfillmentTransition("order cannot expire in its current state")
        reservations = list((await self.session.scalars(select(Reservation).where(Reservation.order_id == order.id).order_by(Reservation.product_id))).all())
        if reservations and any(r.expires_at is not None and r.expires_at > moment for r in reservations if r.status == "reserved"):
            raise InvalidFulfillmentTransition("reservation has not expired")
        # Calling the locked implementation keeps order -> reservation -> balance lock order.
        await self._release_locked(order, status="expired", at=moment)
        old = order.fulfillment_status
        order.fulfillment_status = "expired"
        order.expired_at = moment
        self.session.add(
            FulfillmentTransition(
                order_id=order.id,
                actor_id=actor.user_id,
                actor_role=actor.role,
                from_status=old,
                to_status="expired",
                reason=reason.strip(),
                transitioned_at=moment,
            )
        )
        if order.payment_status == "paid":
            order.reconciliation_required = True
        await self.session.commit()
        return TransitionResult(order, True)

    async def record_payment(
        self,
        public_id: str,
        *,
        amount: int,
        currency: str = "IRR",
        provider: str = "fake",
        provider_event_id: str | None = None,
        provider_transaction_id: str | None = None,
        payload_fingerprint: str | None = None,
    ) -> PaymentAttempt:
        order = await self._locked_order(public_id)
        existing = None
        if provider_event_id is not None:
            existing = await self.session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.provider == provider, PaymentAttempt.provider_event_id == provider_event_id).with_for_update()
            )
        if existing is None and provider_transaction_id is not None:
            existing = await self.session.scalar(
                select(PaymentAttempt)
                .where(PaymentAttempt.provider == provider, PaymentAttempt.provider_transaction_id == provider_transaction_id)
                .with_for_update()
            )
        if existing is not None:
            if payload_fingerprint is not None and existing.payload_fingerprint != payload_fingerprint:
                raise ValueError("payment event payload conflict")
            await self.session.commit()
            return existing
        moment = datetime.now(UTC)
        if order.fulfillment_status not in {"cancelled", "expired", "shipped", "delivered"}:
            reservations = await self._reservations(order.id)
            if any(r.status == "reserved" and r.expires_at is not None and r.expires_at <= moment for r in reservations):
                old = order.fulfillment_status
                await self._release_locked(order, status="expired", at=moment)
                order.fulfillment_status = "expired"
                order.expired_at = moment
                order.reconciliation_required = True
                self.session.add(
                    FulfillmentTransition(
                        order_id=order.id,
                        actor_id=None,
                        actor_role="payment",
                        from_status=old,
                        to_status="expired",
                        reason="payment arrived after reservation expiry",
                        transitioned_at=moment,
                    )
                )
        if order.fulfillment_status in {"cancelled", "expired"}:
            order.reconciliation_required = True
            attempt = PaymentAttempt(
                order_id=order.id,
                provider=provider,
                provider_event_id=provider_event_id,
                provider_transaction_id=provider_transaction_id,
                amount=amount,
                currency=currency,
                status="reconciliation",
                payload_fingerprint=payload_fingerprint,
            )
            self.session.add(attempt)
            await self.session.commit()
            return attempt
        if order.amount is not None and (amount != order.amount or currency != (order.currency or "IRR")):
            raise ValueError("payment amount or currency does not match order")
        if order.payment_status == "paid":
            await self.session.commit()
            existing = await self.session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.order_id == order.id, PaymentAttempt.status == "succeeded").order_by(PaymentAttempt.id.desc())
            )
            if existing is None:
                raise RuntimeError("paid order has no payment attempt")
            return existing
        attempt = PaymentAttempt(
            order_id=order.id,
            provider=provider,
            provider_event_id=provider_event_id,
            provider_transaction_id=provider_transaction_id,
            amount=amount,
            currency=currency,
            status="succeeded",
            payload_fingerprint=payload_fingerprint,
        )
        self.session.add(attempt)
        order.payment_status = "paid"
        await self.session.commit()
        return attempt


def csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


class ReportService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def revenue(
        self,
        *,
        start: datetime,
        end: datetime,
        seller_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        cursor: int | None = None,
    ) -> dict[str, object]:
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("start/end must be timezone-aware and start before end")
        limit = max(1, min(limit, 100))
        filters = [
            Order.created_at >= start,
            Order.created_at < end,
            Order.payment_status == "paid",
            Order.fulfillment_status.notin_(["cancelled", "expired"]),
            Order.reconciliation_required.is_(False),
        ]
        if seller_id is not None:
            filters.append(Order.created_by_id == seller_id)
        if status is not None:
            filters.append(Order.fulfillment_status == status)
        page_filters = list(filters)
        if cursor is not None:
            page_filters.append(Order.id > cursor)
        query = select(Order).where(*page_filters).order_by(Order.id).limit(limit)
        rows = list((await self.session.scalars(query)).all())
        report_filters = list(filters)
        total = await self.session.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(*report_filters))
        order_count = await self.session.scalar(select(func.count(Order.id)).where(*report_filters))
        return {
            "currency": "IRR",
            "order_count": int(order_count or 0),
            "revenue": int(total or 0),
            "items": rows,
            "next_cursor": rows[-1].id if len(rows) == limit else None,
        }

    async def csv(
        self,
        *,
        start: datetime,
        end: datetime,
        seller_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        cursor: int | None = None,
    ) -> str:
        report = await self.revenue(start=start, end=end, seller_id=seller_id, status=status, limit=limit, cursor=cursor)
        out = io.StringIO(newline="")
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(["order_id", "created_at", "seller_id", "fulfillment_status", "amount", "currency"])
        for order in cast(list[Any], report["items"]):
            writer.writerow(
                [
                    csv_safe(order.public_id),
                    csv_safe(order.created_at.isoformat()),
                    csv_safe(order.created_by_id),
                    csv_safe(order.fulfillment_status),
                    csv_safe(order.amount),
                    csv_safe(order.currency),
                ]
            )
        return out.getvalue()

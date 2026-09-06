"""PostgreSQL outbox claiming and a testable Telegram delivery process."""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import Order, Outbox
from backend.services.orders import OrderService


class DeliveryTransport(Protocol):
    async def send_photo(self, order: Order) -> int: ...
    async def send_text(self, order: Order, photo_message_id: int) -> int | None: ...


class AmbiguousDeliveryError(Exception):
    """The provider may have accepted the request; automatic replay is unsafe."""


class TransientDeliveryError(Exception):
    def __init__(self, message: str = "transient delivery failure", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class DeliveryClaim:
    order_id: int
    claim_token: str
    attempts: int


class DeliveryService:
    MAX_ATTEMPTS = 5
    MAX_RETRY_HINT_SECONDS = 3600
    BASE_BACKOFF_SECONDS = 2

    def __init__(self, session: AsyncSession):
        self.session = session

    @classmethod
    def _backoff(cls, attempts: int, retry_after: float | None = None) -> datetime:
        # Jitter is deliberately small and bounded. Tests can pass a retry hint
        # of zero and inspect the persisted schedule without sleeping.
        exponential = min(300, cls.BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)))
        hinted = min(cls.MAX_RETRY_HINT_SECONDS, max(0.0, retry_after or 0.0))
        delay = max(exponential + random.uniform(0, min(1, exponential / 4)), hinted)
        return datetime.now(UTC) + timedelta(seconds=delay)

    async def claim_next(self, *, worker_id: str, lease_seconds: int = 300) -> DeliveryClaim | None:
        now = datetime.now(UTC)
        row = await self.session.scalar(
            select(Outbox)
            .where(
                or_(
                    Outbox.status == "pending",
                    and_(Outbox.status == "failed", Outbox.next_attempt_at.is_not(None)),
                ),
                or_(Outbox.next_attempt_at.is_(None), Outbox.next_attempt_at <= now),
            )
            .order_by(Outbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            await self.session.rollback()
            return None
        if row.attempts >= self.MAX_ATTEMPTS:
            row.status, row.error_code, row.next_attempt_at = "failed", "attempt_limit", None
            await self.session.commit()
            return None
        token = uuid.uuid4().hex
        row.status, row.worker_id, row.claim_token = "sending", worker_id, token
        row.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
        row.attempts += 1
        await self.session.commit()
        return DeliveryClaim(row.order_id, token, row.attempts)

    async def load_order(self, order_id: int) -> Order | None:
        return await self.session.get(Order, order_id)

    async def mark_photo_sent(self, claim: DeliveryClaim, message_id: int) -> None:
        await OrderService(self.session).mark_photo_sent(claim.order_id, claim.claim_token, message_id)

    async def mark_sent(self, claim: DeliveryClaim, photo_id: int, text_id: int | None) -> None:
        await OrderService(self.session).mark_delivery_sent(claim.order_id, claim.claim_token, photo_id, text_id)

    async def mark_failed(self, claim: DeliveryClaim, error_code: str, retry_after: float | None = None) -> None:
        # Fetching the attempt count is safe because mark_delivery_failed fences
        # the claim token; retry timing is bounded in one place.
        retry_at = self._backoff(claim.attempts, retry_after) if claim.attempts < self.MAX_ATTEMPTS else None
        await OrderService(self.session).mark_delivery_failed(
            claim.order_id, claim.claim_token, error_code, retry_at=retry_at
        )

    async def mark_ambiguous(self, claim: DeliveryClaim, error_code: str = "telegram_timeout") -> None:
        await OrderService(self.session).mark_delivery_ambiguous(claim.order_id, claim.claim_token, error_code)


class DeliveryWorker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], transport: DeliveryTransport, *, worker_id: str | None = None):
        self.session_factory = session_factory
        self.transport = transport
        self.worker_id = worker_id or uuid.uuid4().hex

    async def run_once(self, *, lease_seconds: int = 300) -> bool:
        async with self.session_factory() as session:
            service = DeliveryService(session)
            claim = await service.claim_next(worker_id=self.worker_id, lease_seconds=lease_seconds)
            if claim is None:
                return False
            order = await service.load_order(claim.order_id)
            if order is None:
                await service.mark_failed(claim, "order_missing")
                return True
            try:
                photo_id = order.channel_photo_message_id
                if photo_id is None:
                    photo_id = await self.transport.send_photo(order)
                    await service.mark_photo_sent(claim, photo_id)
                text_id = await self.transport.send_text(order, photo_id)
                await service.mark_sent(claim, photo_id, text_id)
            except AmbiguousDeliveryError:
                await service.mark_ambiguous(claim)
            except (TimeoutError, asyncio.TimeoutError, ConnectionError):
                await service.mark_ambiguous(claim)
            except TransientDeliveryError as exc:
                await service.mark_failed(claim, "transient", exc.retry_after)
            except Exception:
                await service.mark_failed(claim, "delivery_error")
            return True


async def run_forever(worker: DeliveryWorker, *, poll_seconds: float = 1.0) -> None:
    while True:
        claimed = await worker.run_once()
        if not claimed:
            await asyncio.sleep(max(0.05, poll_seconds))

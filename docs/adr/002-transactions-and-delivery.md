# ADR 002: PostgreSQL transaction, idempotency, and delivery ownership

- Status: accepted design for Plan 004 stage 1
- Scope: order creation and Telegram channel delivery

## Stored data

PostgreSQL preserves legacy `admins.telegram_id`, admin code uniqueness and
active state, order IDs and `public_id`, `draft_token`, UTC timestamps,
normalized and raw phone/product values, customer/address snapshots, positive
quantity, nullable integer amount, notes, `photo_file_id`, `duplicate_of`,
delivery status/attempt/error fields, Telegram photo/text message IDs, and
`delivered_at`. Legacy IDs are imported explicitly and sequences reset after
import. Historical free-text products remain valid; no SKU or price is
invented. Future catalog rows and Plan 005 order-item price/name snapshots are
additive.

## Order transaction

Use PostgreSQL READ COMMITTED. Before checking the duplicate window, acquire a
transaction-scoped advisory lock derived from the normalized phone/product
pair. This serializes only competing business keys without requiring
SERIALIZABLE snapshot retries. The transaction then:

1. verifies the actor is active;
2. checks the scoped idempotency record;
3. checks the configured duplicate window;
4. inserts the order when allowed;
5. inserts the outbox job; and
6. commits all changes together.

`draft_token` remains unique for legacy compatibility. API idempotency uses a
unique `(actor_id, operation, key)` constraint and stores a canonical payload
hash plus resulting order ID. The same key and hash returns the original
result. A changed payload returns a conflict and creates nothing. A recent
duplicate with `allow_duplicate=false` returns only confirmation-required
metadata; confirmed duplicates point to the recent order through
`duplicate_of`.

The order, idempotency row, and outbox row are atomic. A failure at any point
rolls back all of them. Validate money and quantity at the service boundary
and with database constraints.

## Delivery

The outbox is durable PostgreSQL state created with the order. Its service
contract is:

```python
claim_delivery(order_id: int, *, worker_id: str, lease_seconds: int) -> DeliveryClaim | None
mark_delivery_sent(order_id: int, claim_token: str, photo_message_id: int,
                   text_message_id: int | None) -> None
mark_delivery_failed(order_id: int, claim_token: str, error_code: str,
                     *, retry_at: datetime | None) -> None
```

Claims use `FOR UPDATE SKIP LOCKED`, a lease owner, expiry, attempt count, and
a claim token used for fencing. A live lease cannot be stolen. Safe failures
before a Telegram request, and explicitly transient responses, may retry with
bounded exponential backoff, bounded attempts, and Telegram rate-limit retry
hints. An uncertain timeout or connection failure after a Telegram operation
may have been accepted is `ambiguous`, requires manual reconciliation, and is
never automatically replayed.

The adapter performs Telegram calls outside the database transaction and keeps
the existing manual retry behavior. Legacy order delivery columns are updated
as a compatibility projection. Logs contain correlation/order IDs and error
classes only; never phone, address, tokens, request bodies, or raw customer
data.


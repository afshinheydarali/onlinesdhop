# Commerce transaction decisions and acceptance fixtures

Supervisor refinement of Plan005, 2026-09-06. Foundation completion is still a prerequisite. These decisions make the authorized fake-payment and stock work concrete; they do not authorize real charges or production migration.

## Domain decisions

- Preserve legacy free-text orders with unknown amount. Catalog orders use IRR integer prices and immutable item/address snapshots; the server computes totals. A separate commerce creation operation can coexist with legacy creation without inventing legacy SKUs.
- Reserve available stock at creation. Consume reserved stock at shipment, not packing. Before shipment, cancellation releases each reservation once. A paid cancellation records refund-required reconciliation; this demo does not execute a real refund or label it refunded without an explicit verified event.
- Fulfillment state, payment state and notification delivery state are separate. Log every fulfillment transition with actor/time/from/to/reason in the same transaction.
- Cancellation, payment and expiry lock the order before touching its reservations. Lock multiple SKUs in sorted order. If database time is at or beyond reservation expiry when a payment is applied, expiry wins; late funds enter reconciliation and do not restore stock or revive the order.
- The deterministic fake gateway signs raw request bytes using a separately supplied HMAC secret. A provider event ID is unique with a payload fingerprint; reusing it with changed content conflicts. A second event ID for the same provider transaction cannot apply payment twice. Bind order, expected amount and currency before mutation. Never trust an unsigned callback status.
- Worker leases have claim tokens, expiry, bounded attempts and retry timing. A live lease cannot be stolen; a stale worker cannot finalize another claim. Telegram transport timeouts may represent accepted sends, so mark ambiguous for explicit reconciliation rather than blindly repeating. Known rate limits may use bounded retry hints. Persist a successfully sent photo before sending a split text message.
- Revenue reports count paid, non-cancelled, non-expired orders according to documented UTC time boundaries. Reconciliation/refund-required rows are excluded. CSV text cells beginning with formula characters, including after whitespace, must be escaped.

## Required deterministic PostgreSQL cases

Use distinct sessions and barriers with timeouts, never timing sleeps. Each reset fixture parses an explicitly configured disposable localhost database name ending `_test` before connecting. Tests must use that same factory for HTTP requests and services.

1. Two buyers compete for one remaining unit: exactly one reservation succeeds; no negative inventory; one complete order/outbox.
2. A multi-item order lacks one SKU: no partial order, item, stock movement, reservation, idempotency or outbox survives.
3. Opposite input SKU order still acquires locks consistently; both nonconflicting purchases finish without deadlock.
4. Same actor/key with identical payload concurrently returns one order and one stock effect. Changed payload conflicts. Different actors have independent key scopes.
5. A subsequent catalog price/name change leaves the historic invoice intact.
6. Repeated cancellation and expiry release once. Payment/expiry and payment/cancellation races follow the above order-lock policy.
7. Valid signed payment applies once. Bad signature, wrong amount/currency/order, changed event replay and repeated transaction with another event ID cause no additional state/stock effect. Late payment records reconciliation.
8. Invalid fulfillment transitions fail atomically. Seller and warehouse roles cannot access financial/admin scope. Shipping consumes once; shipped orders reject ordinary cancellation.
9. Two worker instances cannot claim one active lease. Expired uncertain sends remain discoverable, stale claim tokens fail, attempt limits stop retries, rate hints are bounded, partial-photo retry sends only text.
10. Report arithmetic matches synthetic fixtures; unpaid/cancelled/expired/reconciliation rows are excluded; pagination and time boundaries are tested; CSV malicious cells remain text.

Portfolio evidence is recorded only from actual clean setup, backup/restore and benchmark commands. Keep machine configuration, dataset/request mix/concurrency/duration, p50/p95, throughput and errors alongside results. Optional coupons, returns, uploads and multi-store work remain backlog, as stated in the original plan.

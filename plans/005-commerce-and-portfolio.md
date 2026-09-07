# Plan 005: Build the commerce capabilities and evidence that make the portfolio credible

Status DONE (2026-09-07). Priority P2, optional extensions P3. Effort L (roughly 6-9 weeks part-time after foundation). Risk HIGH for stock/payment correctness. Category product direction/operations. Planned at f8900a8, 2026-09-04. Depends on 004. All four slices are implemented; local fresh-migration, restore, demo, full-suite, lint, type, dependency, and migration-drift gates are recorded in `plans/EXECUTION.md`.

## Context and prerequisite contract

The repository began as a private aiogram order bot. The integrated backend now adds a PostgreSQL catalog, transactional reservations, authenticated API, fulfillment transitions, revenue reports, signed fake payment callbacks, durable delivery worker, restore drill, and portfolio evidence. The original Persian bot flow remains supported through shared services.

Run `git diff --stat f8900a8..HEAD -- backend migrations order_bot tests compose.yaml docs` and read the actual ADRs/service contracts created by 004. Its additions are expected drift. If the prerequisite API, permission model and PostgreSQL tests do not exist, stop instead of inventing a parallel implementation. Preserve Persian Telegram messages, UTC persistence, explicit integer currency amounts and privacy by role.

Allowed: backend/, migrations/, tests/, scripts/ for synthetic seed and benchmark, docs/, README.md, Docker/Compose/CI and small Telegram-adapter calls. Out of scope: a full frontend, real charges/refunds, production data, multi-store tenancy, Kafka/Kubernetes/Elasticsearch and AI features.

## Verification contract

Existing baseline command: `python -B -m unittest discover -v`. Carry forward lint/typecheck and PostgreSQL integration commands introduced by prerequisites. The commands below are proposed modules to create in this plan; they were not available or run during the audit. All tests use a disposable PostgreSQL database, fake/sandbox integrations and no real customer records.

## Slice 1 — Catalog, multi-item orders and stock (about 3 weeks)

Add Product/SKU, OrderItem, InventoryBalance/Reservation and minimal stock movement history. SKU unique; quantity positive; stock non-negative. Order items snapshot SKU/name/unit price/currency; changing the catalog must not change an old invoice. Address is snapshotted for the order. Compute totals server-side using integers and one explicit currency (IRR initially); never trust totals from API clients. Define legacy orders with unknown amount explicitly instead of treating null as zero.

Reserve stock in a transaction via conditional update or row locking. Order, items, reservation and outbox insertion must succeed/fail together. Lock multiple SKUs in deterministic order. Idempotency scope is user/store operation plus key and canonical payload fingerprint; identical repeat returns the same order, different payload with same key conflicts.

Verify `python -m unittest tests.integration.test_inventory tests.integration.test_order_idempotency -v` -> two concurrent transactions purchasing the final unit yield exactly one accepted reservation; duplicate requests have one order and one stock effect; multi-item shortage rolls back all reservations; catalog price changes do not mutate snapshots. Use separate DB sessions and synchronization barriers, not sleep-based races.

## Slice 2 — Persistent delivery worker and sandbox payment (about 2 weeks)

Implement an outbox worker in the same repository; PostgreSQL remains the durable source. Claim with leases, bounded batch size, attempt limit and next_attempt_at. Retry transient errors with bounded exponential backoff/jitter; handle rate-limit retry hints; distinguish terminal failure and ambiguous delivery. Restart recovery must not steal live leases. Preserve Telegram photo IDs and the manual reconciliation path. A separate broker is optional, not required for this slice.

Define PaymentAttempt and processed webhook events with uniqueness per provider event/transaction. Start with a deterministic fake gateway and its documented signature protocol; select a real provider's sandbox only after region/provider is known. Verify signature using raw request bytes, amount/currency/order binding, permitted state transitions and deduplication. A callback parameter saying 'success' is not payment verification. Handle out-of-order and late events without reviving cancelled/expired orders silently; route inconsistent cases to reconciliation.

Verify `python -m unittest tests.integration.test_outbox tests.integration.test_payments -v` -> duplicate webhook has one effect, invalid signature/amount changes no state, two workers cannot claim the same active lease, timeout/restart keeps jobs discoverable, retry count is bounded, and injected failure after commit does not lose the job. Network exactly-once is explicitly not a done criterion.

## Slice 3 — Fulfillment, reports and scoped operations (about 1-2 weeks)

Separate fulfillment from payment and delivery-notification status. Define allowed transitions: draft -> confirmed -> packing -> shipped -> delivered, with cancellation rules before shipment. Payment states are pending/paid/failed/refunded independently. Record actor/time/from/to/reason for status changes in the same transaction. Cancel/expiry releases reservation exactly once; late payment after expiry requires explicit reconciliation.

Add manager filters by time/status/seller, bounded pagination, warehouse tracking updates with minimal customer information, owner failed-delivery view, and sales CSV. Revenue excludes unpaid/cancelled orders according to an explicit report definition. Use SQL aggregation and inspect actual query plans before adding indexes/cache. Make exported text safe against spreadsheet formula interpretation.

Verify `python -m unittest tests.integration.test_fulfillment tests.integration.test_reports -v` -> invalid transitions rejected, unauthorized roles cannot see other orders/PII, duplicate cancel restores stock once, expiry/payment race obeys documented policy, and synthetic report totals match fixture arithmetic. Test pagination boundaries and CSV safety.

## Slice 4 — Operational and portfolio evidence (about 1-2 weeks)

Add structured logs with correlation/order IDs and no phone/address/token/body dumps. Health/readiness should reflect process/dependency readiness appropriately. Measure request latency/error rate, delivery backlog/age/retry count and DB pool pressure. Create backup/restore instructions and a tested restore into a separate disposable database.

Add deterministic synthetic seed, English README, ERD, two or three ADRs explaining transaction/idempotency/delivery tradeoffs, API examples, setup command and a 3-5 minute demo script. Demo: create from Telegram and API; compete for last stock unit; inject delivery failure; recover worker; replay sandbox webhook; inspect permitted order status.

Create a reproducible load script or documented tool configuration. Record machine limits, dataset size, request mix, concurrency, duration, p50/p95, throughput, error rate and query counts. Choose a performance budget after a baseline; do not invent '10k RPS' or production-scale claims. Restrict destructive demo endpoints and use synthetic resettable data; keep real Telegram secrets off public demo surfaces.

Verify `python -m unittest tests.integration.test_restore tests.integration.test_demo_smoke -v` -> restored IDs/counts/constraints and smoke flows pass. Verify setup from a clean checkout and the documented benchmark command -> report artifact with actual measurements, then run the full CI gates. The local benchmark artifact records the measured run; external CI remains a pending integration gate until the final branch is pushed by the supervisor. Publishing demo infrastructure is a separate concrete action after this reviewable deliverable exists.

## Optional backlog and maintenance

Only after the slices work: bounded-use coupons with concurrency tests, returns/refunds, email notifications, persistent draft FSM with TTL, S3-compatible uploads, multi-store isolation. Pick the next feature using user value and evidence. Stock/payment/report logic belongs in shared services, not duplicated handlers. Every new transition must extend the transition tests; every new role needs negative authorization tests.

## Stop conditions and git

Stop on real payment credentials/charges, unresolved refund policy, production migration, unexplained data-loss risk or failing prerequisites. Use conventional commits per working slice. No production push/deploy. This plan is DONE after the functional gates and portfolio artifacts recorded in the execution log.

# Fulfillment and revenue operations

Orders carry three independent states:

* `fulfillment_status`: `draft -> confirmed -> packing -> shipped -> delivered`,
  with `cancelled` or `expired` terminal before shipment.
* `payment_status`: `pending`, `paid`, `failed`, or `refunded`.
* `delivery_status`: the Telegram notification outbox state (`pending`,
  `sending`, `sent`, `failed`, or `ambiguous`).

`PATCH /api/v1/orders/{public_id}/fulfillment` accepts `{status, reason}`. It is
available to owner, manager, and warehouse roles; warehouse can perform the
packing, shipping, and delivery operations and cannot cancel an order. Each
successful transition stores actor, role, UTC time, previous state, next state,
and reason in `fulfillment_transitions` in the same database transaction. A
shipping transition consumes each reservation once and subtracts both
`on_hand` and `reserved`; cancellation or expiry releases each reservation
once. A shipped order cannot be ordinarily cancelled.

`GET /api/v1/reports/revenue` and its `.csv` variant are owner/manager routes.
They use UTC start-inclusive/end-exclusive `start` and `end` query values and
stable ascending order IDs with a bounded `limit` and `cursor`. Revenue counts
only paid orders that are not cancelled or expired and have no
`reconciliation_required` flag. CSV values beginning with `=`, `+`, `-`, or `@`
after leading whitespace are prefixed with an apostrophe before writing.

Paid cancellation and payment received after expiry set reconciliation required;
the service records that work remains and never claims a refund was executed.

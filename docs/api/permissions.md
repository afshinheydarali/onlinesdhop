# API v1 permission contract

This file is the route-review checklist for Plan 004. Each route must name one
of these operations and use the authenticated actor from the database. No
route may accept a caller-supplied role, seller identity, or authorization
decision.

| Operation | owner | manager | seller | warehouse |
|---|---:|---:|---:|---:|
| `POST /api/v1/orders` | yes | yes | yes | no |
| `GET /api/v1/orders/{public_id}` | yes | yes | own recovery metadata | fulfillment fields only |
| `GET /api/v1/orders` | yes | operational scope | own recovery metadata | bounded fulfillment scope |
| `POST /api/v1/admins` | yes | no | no | no |
| `PATCH /api/v1/admins/{telegram_id}` | yes | no | no | no |
| `PATCH /api/v1/orders/{public_id}/fulfillment` | yes | yes | no | yes, minimum fields |
| `GET /health/live` | public process check | public process check | public process check | public process check |

Financial revenue reports (`GET /api/v1/reports/revenue` and
`GET /api/v1/reports/revenue.csv`) are owner/manager only. Fulfillment detail
and transitions are owner/manager/warehouse only; sellers retain their own
recovery order views and receive no payment, address, or customer fields.

Seller responses expose only recovery data for orders they created: public ID,
creation time, delivery status, attempt outcome, and retry eligibility. They do
not expose another seller's existence, customer name, phone, address, or
historic order list. A forbidden object lookup may return 404 without details.
Warehouse views deliberately contain only the shipping fields needed for
fulfillment; payment, seller administration, and unrelated customer history
are excluded.

Authentication requirements:

- Missing, malformed, expired, or invalidly signed JWT: `401`.
- Valid token for an inactive user or mismatched `token_version`: `401`.
- Authenticated actor lacking the operation: `403`, or non-disclosing `404` for
  object access.
- Malformed or invalid business payload: `422`.
- Reused idempotency key with a different canonical payload: `409`.

The login failure limiter uses the direct socket IP and username in process
memory. It does not trust `X-Forwarded-For` and is bounded with expiry and a
fixed entry cap. Deployments with multiple API processes or replicas must add
an equivalent shared or edge rate limit; the in-process limiter is not a
cross-process security boundary.

Role lookup is server-side on every request. Token claims cannot elevate a
seller to manager, and deactivation/revocation takes effect without waiting
for token expiry. Any new route must add a row to the matrix and a negative
case before implementation is considered complete.

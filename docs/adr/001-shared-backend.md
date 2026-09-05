# ADR 001: Shared backend boundary and authorization

- Status: accepted design for Plan 004 stage 1
- Scope: the modular monolith that serves the Telegram adapter and FastAPI
- Depends on: the existing `order_bot` behavior and the PostgreSQL migration

## Decision

Business operations live in `backend/services`; FastAPI routes and the
aiogram bot are adapters. Both adapters call the same concrete order service.
The bot must use that service in PostgreSQL mode. The existing SQLite path is
retained during migration and calls the same service contract through an
explicit async adapter (a complete SQLite call may run in a worker thread,
with its connection created and closed there). Do not add a second set of
order validation or creation rules in either adapter, and do not ship an
unused demonstration adapter.

The first concrete service contract is:

```python
create_order(command: CreateOrderCommand, actor: Actor) -> CreateOrderResult
get_order(public_id: str, actor: Actor) -> OrderView | None
list_orders(actor: Actor, *, cursor: str | None, limit: int) -> Page[OrderView]
set_admin_active(telegram_id: int, active: bool, actor: Actor) -> None
list_admins(actor: Actor) -> list[AdminView]
```

`CreateOrderCommand` preserves the current keys and meanings: `customer_name`,
`phone_raw`, `phone_normalized`, `province`, `city`, `address`, optional
`postal_code`, `product_raw`, `product_normalized`, positive `quantity`,
nullable integer `amount`, optional `notes`, `photo_file_id`, an idempotency
key, and `allow_duplicate`. A null amount remains unknown; it is never treated
as zero. The bot keeps its Persian prompts, caption parsing, preview, and
HTML rendering keys. Telegram file IDs remain channel-specific metadata.

## Identity and roles

Authentication uses local password hashes with `pwdlib` Argon2 and signed JWTs
using PyJWT. A token carries identity and expiry/session claims, but role and
seller ownership are read from the database. Every request re-checks that the
user is active and that the token's `token_version` still matches, allowing
immediate deactivation or revocation. Telegram identity is verified by the
adapter and mapped to the internal user; request fields cannot choose the
acting user or role.

| Role | Allowed operations | Data boundary |
|---|---|---|
| owner | manage users; all operational views | full access |
| manager | operate and review orders | operational order data |
| seller | create orders; retry/recover own delivery | own recovery metadata only; no historic customer PII |
| warehouse | update fulfillment/shipping fields | minimum shipping data needed to fulfill |

Every route must be classified in this matrix. Unclassified routes fail the
permission test. Cross-owner lookups may return a non-disclosing 404.

## Consequences

The service owns validation, active-user checks, duplicate handling,
idempotency, and persistence. Adapters own transport and presentation. New
commerce behavior in Plan 005 extends services and tests rather than handlers.
Any expansion of seller history or warehouse data is a separate product
decision with a negative authorization test.


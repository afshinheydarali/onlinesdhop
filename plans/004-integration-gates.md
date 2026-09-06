# Foundation integration work packages

Supervisor refinement, 2026-09-06, integration baseline 6927a73. This supplements Plan004; it does not mark the unfinished foundation complete. Execute after reviewed delivery recovery and backend/import commits are integrated. The user authorized Luna executors and GitHub commits; the supervisor reviews and pushes verified slices.

## Package A: production Telegram service adapter

Scope: order_bot configuration and async persistence adapter, backend concrete bot adapter, related services only where a shared operation is missing, tests and adapter documentation. Preserve Persian prompts, current draft/revision checks and seller privacy.

1. Add optional DATABASE_URL configuration. Production startup selects PostgreSQL when present; otherwise retains SQLite. PostgreSQL migrations are explicit deployment steps, never automatic destructive startup work.
2. Change handler persistence calls to await a typed adapter. SQLite wraps each complete existing Database method with asyncio.to_thread; connections remain created/closed inside Database methods. Keep Database's synchronous public API for legacy tests and importer fixtures. Tests exercise the actual production async adapter; do not add blocking compatibility branches in handlers.
3. PostgreSQL creation maps verified Telegram ID to active User and Admin, builds CreateOrderCommand from the confirmed snapshot and uses OrderService.create_order. Draft token becomes actor-scoped idempotency key. Recover/retry only caller-owned rows. Owner administration uses configured owner authority at the Telegram boundary and shared validated persistence operations; ordinary imported admins remain sellers. No implicit promotion to manager.
4. PostgreSQL order creation commits one outbox row. The bot reports queued work and exposes persisted recovery metadata; a worker owns network publication. Do not have the bot independently send a PostgreSQL order while a worker may claim it. Preserve immediate sending in SQLite mode. Document this accepted adapter distinction.
5. Persist admin/user creation/deactivation atomically. Re-enable must not accidentally restore an independently revoked account. Define and test the selected explicit reactivation semantics.
6. Real routed synthetic Telegram tests against PostgreSQL prove form confirmation calls the shared service, creates one order/outbox, does not send the channel message inline, rejects inactive identities, preserves new drafts and supports recovery with a fresh dispatcher. Equivalent API and bot inputs preserve normalization, nullable money, duplicate behavior and idempotency. Mock only Telegram transport.

Gates: full existing unittest suite, PostgreSQL adapter tests with no skipped cases, Ruff and mypy. Tests parse the destination URL and permit resets only for a dedicated localhost database ending `_test`; never infer safety from the password or full URL substring. Commit the verified package, leave plans to the supervisor.

## Package B: reproducible deployment and CI

Scope: dependencies/locks, Docker/Compose, CI, environment example and setup documentation. No feature implementation.

1. Pin a compatible Python 3.11/3.12 backend runtime alongside the existing aiogram lock. Keep tools in development installation; verify pip check. Do not commit virtualenvs or local test credentials.
2. Add Compose PostgreSQL readiness, explicit migration job and API service; retain optional Telegram service requiring real credentials only when enabled. Persist database storage. API and later worker share DATABASE_URL; JWT_SECRET is required and supplied externally. Healthchecks must use utilities present in the image. Do not use a dummy known production secret.
3. CI provisions disposable PostgreSQL, runs Alembic upgrade head/check, all integration cases with no silent skips, legacy unit tests, Ruff, mypy and dependency checks on both Python versions. Test guards accept the configured CI localhost service. Include importer and bot adapter cases after integration.
4. Verify constrained installation in an isolated clean environment. Run local API startup and GET health/live, health/ready and openapi.json using synthetic settings. Validate Compose configuration without exposing substituted secrets. If Docker daemon is unavailable, report image/Compose execution as unverified and rely on actual CI where available; do not claim local containers ran.

No production deployment or real Telegram sends. The full foundation remains IN PROGRESS until both packages and previous PostgreSQL transaction, auth, import and migration gates are independently reviewed.

# Plan 004: Design and deliver a shared API/bot backend with PostgreSQL

Status DONE (verified 2026-09-07). Priority P2 after correctness work. Effort L (roughly 3 weeks part-time). Risk HIGH for data migration, MED for additive API. Category direction/architecture. Planned at f8900a8, 2026-09-04. Depends on 001-003. Delivered as staged vertical slices with PostgreSQL, FastAPI, a transactional importer, the Telegram adapter, reproducible deployment configuration, and CI.

Execution scheduling clarification (supervisor, 2026-09-04): 001-003 are integration and completion prerequisites. Additive schema, API/authentication and service implementation may proceed in an isolated worktree alongside bot fixes, using the accepted ADRs. That lane must not edit existing bot behavior or assume its tests passed. Bot integration, combined regression verification and DONE status remain gated on 001-003. This separates independent work without weakening the acceptance criteria.

## Current context and drift

Repo D:/projects/onlineshop is an internal aiogram order-entry bot, not a public shop API. database.py contains admins and a single-product orders table. bot.py combines validation orchestration, workflow, presentation and delivery. Baseline tests use unittest with temporary SQLite and mocked network. The original product deliberately limits sellers' access to historic customer data.

Run `git diff --stat f8900a8..HEAD -- order_bot tests requirements.txt Dockerfile compose.yaml docs` and read prerequisite changes. Expected source anchors:

~~~python
# database.py:134
def save_order(self, admin_id: int, draft: Mapping[str, Any], *, allow_duplicate: bool) -> SaveResult:
# database.py:138
connection.execute("BEGIN IMMEDIATE")
# database.py:151
WHERE phone_normalized = ? AND product_normalized = ? AND created_at >= ?
# database.py:64-67: one product and optional amount
product_raw TEXT NOT NULL,
product_normalized TEXT NOT NULL,
quantity INTEGER NOT NULL CHECK (quantity > 0),
amount INTEGER,
~~~

draft_token is unique, public_id is unique, admin identifiers are unique, foreign keys are enabled and query parameters are bound. Preserve these invariants. SQLite BEGIN IMMEDIATE currently serializes check-plus-insert across writers; PostgreSQL migration must replace the concurrency guarantee explicitly, not just copy SQL.

## Target and boundaries

One repository, modular monolith. Add backend/ with api/, services/, models/ and persistence modules only as actual features need them. Existing order_bot remains the Telegram adapter. Use FastAPI/Pydantic, SQLAlchemy 2, Alembic and PostgreSQL. Use async driver/session for I/O in async handlers; one session per request/task. Do not introduce a generic repository framework or separate service per table.

Allowed new paths: backend/, migrations/, alembic.ini, tests/integration/, scripts/import_sqlite.py, docs/adr/, docs/api/, dependency/CI/Compose config. Modify order_bot only to delegate application work and adapt configuration. Preserve old validation and characterization tests. Out of scope: live payment, multi-tenancy, frontend app, Kubernetes, search cluster, production migration/deployment.

## Commands and expected outputs

Existing verified gate: `python -B -m unittest discover -v` -> all tests pass. The following are proposed gates, not commands that exist at audit time; the executor must implement and document them before declaring success:

- `python -m unittest discover -s tests/integration -v` -> all tests run against an explicitly configured disposable PostgreSQL DB, no skips in CI.
- `python -m alembic upgrade head` -> migrations succeed on a disposable empty DB and supplied test fixture revision.
- `python -m alembic check` -> no unrepresented model/schema changes.
- `python -m uvicorn backend.api.app:app --host 127.0.0.1 --port 8000` -> local startup; /health/live and /openapi.json return 200 in smoke tests.
- `python -m scripts.import_sqlite --help` -> explicit source, destination and dry-run options. Never default to the production DB.

## Stages

1. Write ADRs for schema, permission matrix, transaction boundaries and delivery ownership. Choose explicit role permissions: owner manages users; manager operates orders; seller creates orders and initially sees only recovery metadata of their own; warehouse sees minimum fulfillment data. Any expansion of seller history access must be a recorded product decision with negative tests. Define API error schema and pagination limits. Verify full current unittest suite -> green; add machine-readable permission cases with a test that rejects unclassified routes.
2. Extract a concrete order service and a typed creation input/result. API and bot must call the same validation/business operation. As an interim SQLite path, move complete DB calls off the event loop with each connection created/closed inside its worker invocation; never pass a connection across threads. Characterize existing duplicate-window and inactive-admin behavior. Verify existing tests plus new service tests -> same outcomes and no duplicate implementation in bot/API.
3. Build PostgreSQL models and versioned migrations preserving legacy admin IDs, public IDs, timestamps, delivery states, photo IDs and nullable amounts. Introduce customer/address snapshots without silently merging people by name/phone. Keep historical free-text product orders valid; do not invent prices/SKUs for them. Enforce money/quantity constraints. Preserve business duplicate behavior using an explicit transaction-scoped strategy for normalized phone/product, plus unique idempotency constraints; test concurrent creation with separate sessions. Verify migration gates and PostgreSQL integration tests -> correct row counts, relationships and invariants on synthetic import fixtures.
4. Implement a read-only-source SQLite importer with dry-run summary, repeat-run behavior and destination transaction rollback on bad rows. Generate synthetic old-schema fixture data, including duplicates and partial delivery. Treat Telegram file_id as channel-specific metadata, not a universal URL. Verify importer integration tests -> unchanged source checksum, correct destination counts/IDs, failed import leaves no partial rows, and second run does not duplicate records. Actual production migration requires backup and a separately reviewed cutover.
5. Add authenticated /api/v1 vertical slice: order creation, authorized retrieval, bounded list, admin operations and health endpoints. Do not accept caller-supplied role or seller identity as authority. Prefer a maintained authentication library/provider selected in an ADR; test credential expiry/revocation and object ownership. Telegram identity remains verified by its adapter and maps to internal user ID. Verify integration suite -> unauthorized 401, forbidden scope 403 or non-disclosing 404, malformed payload 422, idempotent repeat returns original result and changed-payload reuse returns 409.
6. Move delivery orchestration toward a persistent outbox record inserted in the same transaction as the order. Define worker ownership, claim lease, retry timing and ambiguous-send policy before running multiple workers. Configure Compose/CI PostgreSQL with readiness checks. Verify bot and API create equivalent service results and the migrated recovery tests from 003 remain green.

## Acceptance, maintenance and stop conditions

The first deliverable is one complete API-to-DB and bot-to-service order flow with tested permissions. No production SQLite deletion and no automatic store-wide data normalization. Stop if legacy data cannot be represented, authentication choice needs unavailable external credentials, prerequisite fixes drift, or preservation of duplicate concurrency is unclear. Keep existing unittest tests during adoption; introduce pytest only if a concrete integration-testing need justifies it, not as a wholesale rewrite.

Branch codex/backend-foundation; commit per verified slice. Do not push or deploy without instruction. Update the plan index after all gates, including migration fixtures and authorization tests, pass.

References: [FastAPI features](https://fastapi.tiangolo.com/features/), [SQLAlchemy async sessions](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html), [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html), [PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html).

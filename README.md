# OnlineShop backend

OnlineShop is a Python 3.11+ backend for a small Telegram and HTTP order workflow. It keeps the existing Persian Telegram customer experience while adding a PostgreSQL backed catalog, multi item orders, inventory reservations, authenticated operations API, and durable delivery outbox. The repository is designed to be readable as a portfolio project: transaction boundaries, privacy rules, migration history, and operational evidence are visible in source and tests.

## Quick start

The API uses PostgreSQL and Alembic. For a local disposable environment, use the portable PostgreSQL tools in `D:\projects\onlineshop-worktrees\postgres-runtime\pgsql\bin` or Docker Compose.

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://onlineshop_test:onlineshop-local-pg18-20260904@localhost:15432/onlineshop_portfolio_test"
$env:TEST_DATABASE_URL = $env:DATABASE_URL
$env:JWT_SECRET = "local-only-change-this-secret"
python -m pip install -r requirements.txt -r requirements-dev.txt -c requirements-lock.txt
python -m alembic upgrade head
python -m scripts.seed_synthetic --reset
python -m uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
```

The seed command is restricted to `localhost` and the named `*_test` databases. It creates two users, two admins, and three products. Never place real customer records or Telegram credentials in this dataset.

Compose starts PostgreSQL, migrations, and the API after the database is healthy:

```powershell
$env:POSTGRES_PASSWORD = "local-password"
$env:DATABASE_URL = "postgresql+asyncpg://onlineshop:local-password@postgres:5432/onlineshop"
docker compose up --build
```

## Architecture

FastAPI (`backend/api`) authenticates requests and maps role permissions to shared order, fulfillment, and report services (`backend/services`). SQLAlchemy models and Alembic migrations define PostgreSQL state. Order creation computes totals from catalog prices, locks products in SKU order, reserves stock, snapshots invoice lines, records idempotency, and inserts an outbox row in one transaction. Fulfillment transitions are audited in the same transaction and revenue reports aggregate paid, non cancelled, non expired orders. A delivery worker can claim outbox rows with leases and bounded retries; the Telegram adapter remains a separate integration boundary.

```mermaid
flowchart LR
  Telegram --> Service[OrderService]
  HTTP[Authenticated HTTP API] --> Service
  Service --> DB[(PostgreSQL)]
  Service --> Outbox[Delivery outbox]
  Outbox --> Worker[Delivery worker / adapter]
  Worker --> Telegram
```

```mermaid
erDiagram
  USERS ||--o{ ORDERS : creates
  ADMINS ||--o{ ORDERS : attributes
  ORDERS ||--o{ ORDER_ITEMS : contains
  PRODUCTS ||--o{ ORDER_ITEMS : snapshots
  PRODUCTS ||--|| INVENTORY_BALANCES : has
  ORDERS ||--o{ RESERVATIONS : holds
  PRODUCTS ||--o{ RESERVATIONS : reserves
  ORDERS ||--o{ STOCK_MOVEMENTS : records
  ORDERS ||--|| OUTBOX : emits
  ORDERS ||--o{ IDEMPOTENCY_KEYS : keys
```

## API examples

Get a token with a seeded account:

```powershell
$token = (Invoke-RestMethod http://127.0.0.1:8000/api/v1/auth/token -Method Post -Body @{username="portfolio-seller"; password="portfolio-test-password"}).access_token
$headers = @{Authorization = "Bearer $token"; "X-Request-ID" = "demo-request-001"}
Invoke-RestMethod http://127.0.0.1:8000/api/v1/products -Headers $headers
```

Create a catalog order. The server calculates the total and reserves stock; clients send no total.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/commerce/orders -Method Post -Headers $headers -ContentType 'application/json' -Body (@{
  customer_name='Synthetic Customer'; phone_raw='09120009999'; province='Tehran'; city='Tehran'; address='Synthetic Street'
  items=@(@{sku='DEMO-RED'; quantity=1}); idempotency_key='demo-order-001'
} | ConvertTo-Json)
```

`GET /health/live` checks process liveness. `GET /health/ready` runs a database query and includes pool state. `GET /metrics` exposes bounded in process HTTP counters plus delivery backlog count, oldest pending age, retry count, and pool state. Logs are JSON metadata containing request and order IDs, method, route, status, and latency. Request bodies, authorization headers, tokens, phone numbers, addresses, and payment bodies are never logged.

Warehouse and manager users can inspect and transition fulfillment through `GET/PATCH /api/v1/orders/{public_id}/fulfillment`; owners and managers can query `/api/v1/reports/revenue` or download its CSV form. Both surfaces use bounded inputs and role checks.

## Guarantees and security boundaries

Orders, item snapshots, reservations, idempotency records, stock movements, and outbox insertion commit or roll back together. Duplicate idempotency keys replay the original order only when their canonical payload matches; a different payload conflicts. Inventory reservations lock all products in deterministic order and enforce non negative database constraints. Seller responses omit customer PII and manager/warehouse access follows the route permission matrix in [`docs/api/permissions.md`](docs/api/permissions.md). Currency is explicit integer IRR in the current catalog flow.

Backups contain personal data and must be access controlled. The backup and restore scripts only accept local `onlineshop_portfolio_test` or `onlineshop_restore_test` targets; restore always targets the latter. See [`docs/backup-restore.md`](docs/backup-restore.md).

## Demo and evidence

Run the deterministic five minute walkthrough in [`docs/demo.md`](docs/demo.md). It covers authentication, API order creation, a final stock race, delivery failure/retry when the worker is available, and webhook replay when a payment route is available. Unavailable optional integrations are reported as skipped and called out as follow up work.

The benchmark is intentionally modest and reproducible. Start the API with the synthetic seed, then run:

```powershell
python scripts/benchmark.py --base-url http://127.0.0.1:8000 --requests 100 --concurrency 4 > docs\benchmark-2026-09-07.json
```

The committed artifact [`docs/benchmark-2026-09-07.json`](docs/benchmark-2026-09-07.json) records the actual machine/runtime, dataset, request mix, concurrency, duration, p50/p95, throughput, errors, and query-count limitation from that run. It is a local baseline, not a production capacity claim.

## Verification

With PostgreSQL running and both dedicated databases migrated:

```powershell
python -m alembic check
python -B -m unittest discover -v
python -m unittest tests.integration.test_restore tests.integration.test_demo_smoke -v
ruff check order_bot backend tests scripts migrations
mypy order_bot backend
python -m pip check
```

The PostgreSQL integration tests refuse non local databases and names outside `onlineshop_portfolio_test` and `onlineshop_restore_test`. Without `TEST_DATABASE_URL`, they skip rather than connect to a default database.

## Decisions and limitations

The concise architecture decisions are [`docs/adr/001-shared-backend.md`](docs/adr/001-shared-backend.md), [`docs/adr/002-transactions-and-delivery.md`](docs/adr/002-transactions-and-delivery.md), and [`docs/adr/003-operational-evidence.md`](docs/adr/003-operational-evidence.md). Real charges, refunds, production deployment, multi store tenancy, and a broker are outside this slice. Delivery and payment demos detect their routes and show a clear skip until those integrations are added.

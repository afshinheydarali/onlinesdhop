# Deterministic portfolio demo (3–5 minutes)

This walkthrough uses only a local synthetic database ending in `_test`. Every reset is explicit and guarded. Run the API in one terminal:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://<user>:<password>@localhost:<port>/<source_test_db>"
$env:TEST_DATABASE_URL = $env:DATABASE_URL
$env:JWT_SECRET = "<local-secret>"
python -m alembic upgrade head
python -m scripts.seed_synthetic --reset
python -m uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
```

In a second terminal, run the smoke flow against PostgreSQL:

```powershell
python -m unittest tests.integration.test_demo_smoke -v
```

For a live presentation, use the API examples in the README to show the request correlation header, catalog price derived server side, idempotent replay, and role filtered order output. Transition the order through `confirmed`, `packing`, `shipped`, and `delivered` with a warehouse token, then use an owner token to query `/api/v1/reports/revenue` (the synthetic order remains unpaid and therefore contributes zero revenue). Open `/health/ready` and `/metrics` to show dependency readiness and delivery backlog telemetry. `python scripts/benchmark.py --requests 100 --concurrency 4` provides a small measured baseline.

The current repository does not expose a payment webhook route or a standalone worker HTTP route. The demo therefore labels payment replay and injected transport failure/recovery as skipped follow up steps. Once those routes are added, extend this script with a deterministic fake transport failure, worker retry, and duplicate signed webhook request.

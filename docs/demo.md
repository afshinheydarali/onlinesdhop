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

For a live presentation, use the API examples in the README to show the request correlation header, catalog price derived server side, idempotent replay, and role filtered order output. Transition the order through `confirmed`, `packing`, `shipped`, and `delivered` with a warehouse token, then use an owner token to query `/api/v1/reports/revenue` and `/api/v1/reports/revenue.csv`. The fake payment callback accepts only a valid HMAC signature over the exact JSON bytes; submit the same signed request twice and show that the second response is a duplicate with no second payment effect.

Run the delivery worker with an injected local transport. Make the transport raise `TransientDeliveryError` once, inspect the persisted `pending` outbox row, transient error code, and retry schedule, set the row due for the demo, then run the worker again and show the same order reaches `sent` while the photo message ID is retained. The deterministic implementation is exercised end to end by `tests.integration.test_demo_smoke`, which also checks public health, protected metrics, signed callback replay, and the failure/recovery transition. Open `/health/ready` for dependency readiness and `/metrics` with an owner token for bounded operational telemetry.

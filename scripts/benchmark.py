"""Small reproducible HTTP benchmark for a running local synthetic API."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime

import httpx


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--username", default="portfolio-seller")
    p.add_argument("--password", default="portfolio-test-password")
    p.add_argument("--requests", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--duration", type=float, default=0, help="optional maximum duration in seconds; 0 means request count")
    args = p.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        raise SystemExit("requests and concurrency must be positive")
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        login = await client.post("/api/v1/auth/token", data={"username": args.username, "password": args.password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        latencies: list[float] = []
        statuses: list[int] = []
        started = time.perf_counter()
        counter = 0
        lock = asyncio.Lock()

        async def one(index: int) -> None:
            # The fixed 70/30 mix is part of the recorded benchmark contract.
            if index % 10 < 7:
                method, path, kwargs = "GET", "/api/v1/products", {"headers": headers}
            else:
                method, path = "POST", "/api/v1/orders"
                kwargs = {
                    "headers": headers,
                    "json": {
                        "customer_name": "Synthetic Customer",
                        "phone_raw": f"0912000{index:04d}",
                        "province": "Tehran",
                        "city": "Tehran",
                        "address": "Synthetic Street",
                        "product_raw": "Demo Red",
                        "quantity": 1,
                        "amount": 120000,
                        "idempotency_key": f"benchmark-{index}",
                        "allow_duplicate": True,
                    },
                }
            begin = time.perf_counter()
            try:
                response = await client.request(method, path, **kwargs)
                statuses.append(response.status_code)
            except httpx.HTTPError:
                statuses.append(599)
            finally:
                latencies.append((time.perf_counter() - begin) * 1000)

        async def worker() -> None:
            nonlocal counter
            while True:
                async with lock:
                    if counter >= args.requests or (args.duration and time.perf_counter() - started >= args.duration):
                        return
                    index = counter
                    counter += 1
                await one(index)

        await asyncio.gather(*(worker() for _ in range(min(args.concurrency, args.requests))))
    elapsed = time.perf_counter() - started
    sorted_latencies = sorted(latencies)

    def percentile(percent: float) -> float | None:
        if not sorted_latencies:
            return None
        index = min(len(sorted_latencies) - 1, int((len(sorted_latencies) - 1) * percent))
        return round(sorted_latencies[index], 3)

    successful = sum(200 <= code < 400 for code in statuses)
    result = {
        "measured_at": datetime.now(UTC).isoformat(),
        "machine": __import__("platform").platform(),
        "python": __import__("platform").python_version(),
        "dataset": "seed_synthetic: 2 users, 2 admins, 3 products; no customer records before run",
        "request_mix": {"GET /api/v1/products": "70%", "POST /api/v1/orders": "30%"},
        "requested_requests": args.requests,
        "completed_requests": len(statuses),
        "concurrency": args.concurrency,
        "duration_seconds": round(elapsed, 3),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "throughput_rps": round(len(statuses) / elapsed, 3) if elapsed else 0,
        "errors": len(statuses) - successful,
        "error_rate": round((len(statuses) - successful) / len(statuses), 6) if statuses else 0,
        "status_counts": {str(code): statuses.count(code) for code in sorted(set(statuses))},
        "query_count": "not instrumented by this HTTP benchmark; use SQLAlchemy event hooks for query-level profiling",
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

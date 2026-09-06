"""Process entrypoint for the PostgreSQL outbox worker.

The transport is injected as ``module:function`` so this package remains
network-free in tests and cannot accidentally send to a real Telegram channel.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
from typing import Any

from backend.db import SessionFactory
from backend.services.delivery import DeliveryWorker, run_forever


def _transport(path: str) -> Any:
    module_name, separator, attr = path.partition(":")
    if not separator or not module_name or not attr:
        raise ValueError("transport must be module:function")
    value = getattr(importlib.import_module(module_name), attr)
    return value() if callable(value) else value


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PostgreSQL delivery outbox worker")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--transport", required=True, help="module:function returning DeliveryTransport")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    await run_forever(
        DeliveryWorker(SessionFactory, _transport(args.transport), worker_id=args.worker_id),
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    asyncio.run(main())

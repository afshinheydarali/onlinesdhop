from __future__ import annotations

import argparse
import asyncio

from backend.auth import hash_password
from backend.db import SessionFactory
from backend.models import User


async def main() -> None:
    p = argparse.ArgumentParser(description="Create the first local owner explicitly")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--telegram-id", type=int, required=True)
    a = p.parse_args()
    async with SessionFactory() as s:
        s.add(
            User(
                username=a.username,
                password_hash=hash_password(a.password),
                telegram_id=a.telegram_id,
                role="owner",
                is_active=True,
                token_version=0,
            )
        )
        await s.commit()


if __name__ == "__main__":
    asyncio.run(main())

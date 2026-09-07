from __future__ import annotations

import argparse
import asyncio
import getpass

from backend.auth import hash_password
from backend.db import SessionFactory
from backend.models import User


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create the first local owner explicitly")
    p.add_argument("--username", required=True)
    p.add_argument("--telegram-id", type=int, required=True)
    return p


async def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    password = getpass.getpass("Owner password: ")
    async with SessionFactory() as s:
        s.add(
            User(
                username=a.username,
                password_hash=hash_password(password),
                telegram_id=a.telegram_id,
                role="owner",
                is_active=True,
                token_version=0,
            )
        )
        await s.commit()


if __name__ == "__main__":
    asyncio.run(main())

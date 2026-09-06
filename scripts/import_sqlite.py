"""Import the legacy SQLite database into PostgreSQL without modifying its source."""
from __future__ import annotations
import argparse, asyncio, hashlib, sqlite3
from datetime import UTC, datetime
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from backend.models import Admin, Base, Order

def checksum(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

async def run(source: str, destination: str, dry_run: bool, reset_seq: bool) -> int:
    before = checksum(source)
    db = sqlite3.connect(f"file:{source}?mode=ro", uri=True); db.row_factory = sqlite3.Row
    admins = db.execute("SELECT * FROM admins ORDER BY telegram_id").fetchall()
    orders = db.execute("SELECT * FROM orders ORDER BY id").fetchall()
    db.close()
    engine = create_async_engine(destination)
    count = 0
    async with engine.begin() as conn:
        if dry_run:
            print(f"source_sha256={before} admins={len(admins)} orders={len(orders)} dry_run=true")
        else:
            for row in admins:
                existing = await conn.execute(select(Admin).where(Admin.telegram_id == row["telegram_id"]))
                found = existing.scalar_one_or_none()
                if found:
                    if found.name != row["name"] or found.admin_code.upper() != row["admin_code"].upper(): raise ValueError(f"admin conflict {row['telegram_id']}")
                else:
                    await conn.execute(Admin.__table__.insert().values(telegram_id=row["telegram_id"], name=row["name"], admin_code=row["admin_code"], is_active=bool(row["is_active"]), created_at=datetime.fromisoformat(row["created_at"])))
            for row in orders:
                found = (await conn.execute(select(Order).where(Order.id == row["id"]))).scalar_one_or_none()
                if found:
                    if found.public_id != row["public_id"] or found.draft_token != row["draft_token"]: raise ValueError(f"order conflict {row['id']}")
                    continue
                values = {c.name: row[c.name] for c in Order.__table__.columns if c.name in row.keys()}
                values["created_at"] = datetime.fromisoformat(row["created_at"])
                await conn.execute(Order.__table__.insert().values(**values)); count += 1
            if reset_seq:
                await conn.execute(text("SELECT setval(pg_get_serial_sequence('orders','id'), COALESCE((SELECT max(id) FROM orders), 1), true)"))
    await engine.dispose()
    if checksum(source) != before: raise RuntimeError("source changed during import")
    print(f"imported_orders={count} source_sha256={before}")
    return count

def main() -> None:
    p = argparse.ArgumentParser(); p.add_argument("--source", required=True); p.add_argument("--destination", required=True); p.add_argument("--dry-run", action="store_true"); p.add_argument("--reset-sequences", action="store_true")
    args = p.parse_args(); asyncio.run(run(args.source, args.destination, args.dry_run, args.reset_sequences))
if __name__ == "__main__": main()

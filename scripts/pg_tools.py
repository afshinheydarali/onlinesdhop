"""Cross platform pg_dump/pg_restore entrypoint for disposable local databases."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from sqlalchemy.engine import URL, make_url

from scripts.db_guard import LOCAL_HOSTS


def _target(raw: str, *, restore: bool = False) -> URL:
    url = make_url(raw)
    if url.host not in LOCAL_HOSTS or not url.database or not url.database.endswith("_test"):
        raise SystemExit("refusing non-local database; use a local database ending in _test")
    if restore and not url.database.endswith("_restore_test"):
        raise SystemExit("restore target must end in _restore_test")
    return url


def resolve_tool(name: str) -> str:
    """Resolve a PostgreSQL client from PG_BIN first, then PATH."""
    candidates = [name + ".exe", name] if os.name == "nt" else [name]
    configured = os.getenv("PG_BIN")
    if configured:
        for candidate in candidates:
            path = Path(configured) / candidate
            if path.is_file():
                return str(path)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise SystemExit(f"{name} not found; install PostgreSQL client tools or set PG_BIN")


def pg_environment(url: URL) -> dict[str, str]:
    if not url.username or not url.host or not url.port:
        raise SystemExit("database URL must include username, host, and port")
    environment = os.environ.copy()
    environment.update({"PGHOST": url.host, "PGPORT": str(url.port), "PGUSER": url.username})
    if url.password is not None:
        environment["PGPASSWORD"] = url.password
    return environment


def backup(raw: str, output: Path) -> None:
    url = _target(raw)
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [resolve_tool("pg_dump"), "--format=custom", "--file", str(output), "--dbname", url.database],
        env=pg_environment(url), check=False
    )
    if completed.returncode:
        raise SystemExit("pg_dump failed")


def restore(raw: str, backup_path: Path) -> None:
    url = _target(raw, restore=True)
    if not backup_path.is_file():
        raise SystemExit(f"backup file not found: {backup_path}")
    environment = pg_environment(url)
    subprocess.run([resolve_tool("createdb"), url.database], env=environment, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    completed = subprocess.run(
        [resolve_tool("pg_restore"), "--clean", "--if-exists", "--no-owner", "--dbname", url.database, str(backup_path)],
        env=environment, check=False
    )
    if completed.returncode:
        raise SystemExit("pg_restore failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--database-url", default=os.getenv("TEST_DATABASE_URL"))
    backup_parser.add_argument("--output", type=Path, required=True)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--database-url", default=os.getenv("RESTORE_DATABASE_URL") or os.getenv("TEST_DATABASE_URL"))
    restore_parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("database URL is required through --database-url or environment")
    if args.command == "backup":
        backup(args.database_url, args.output)
    else:
        restore(args.database_url, args.backup)


if __name__ == "__main__":
    main()

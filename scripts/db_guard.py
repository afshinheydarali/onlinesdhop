"""Safety helpers shared by disposable synthetic database commands."""

from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def guarded_url(env_name: str = "TEST_DATABASE_URL") -> URL:
    raw = os.getenv(env_name)
    if not raw:
        raise SystemExit(f"{env_name} is required and must target a disposable local *_test database")
    url = make_url(raw)
    if url.host not in LOCAL_HOSTS or not url.database or not url.database.endswith("_test"):
        raise SystemExit(f"refusing database {url.database!r}; only local *_test databases are allowed")
    return url

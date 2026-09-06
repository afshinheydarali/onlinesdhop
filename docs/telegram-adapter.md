# Telegram persistence adapter

The bot selects PostgreSQL when `DATABASE_URL` is set and otherwise uses the
legacy SQLite file from `DATABASE_PATH`. SQLite persistence calls each complete
legacy `Database` operation in `asyncio.to_thread`; its synchronous API remains
available to importers and older callers.

PostgreSQL confirmation maps the verified Telegram ID to an active `User` and
`Admin`, then calls `OrderService.create_order`. The order, actor scoped
idempotency record and outbox row commit together. The bot reports that the
order is queued and does not send the channel message inline; an outbox worker
owns that network publication. SQLite keeps its immediate publication behavior
during the migration period.

Run Alembic migrations explicitly before starting the PostgreSQL bot:

```text
DATABASE_URL=postgresql+asyncpg://... python -m alembic upgrade head
```

Disabling an admin also deactivates its linked API user atomically and bumps
the token version. Re-enabling the admin only re-enables the Telegram admin;
it never silently reactivates a user independently revoked through the API.

# Legacy SQLite migration runbook

Use a consistent SQLite backup snapshot. A source file with a live `-wal`
sidecar is rejected; create the backup with SQLite's backup API first so the
database and WAL are captured consistently. The importer opens the snapshot
read-only and checks its SHA-256 before and after the run.

Apply migrations to a disposable PostgreSQL database, then run:

```text
python -m alembic upgrade head
python -m scripts.import_sqlite --source backup.sqlite --destination postgresql+asyncpg://.../onlineshop_import_test
```

`--dry-run` validates every source row and checks destination conflicts and
repeat-run projections by executing the same explicit-ID writes inside a
transaction that is always rolled back; it prints counts without writing
destination data or changing sequences. The import transaction rolls back all
admins, users, orders and outbox rows if any validation or destination conflict
occurs. Existing rows must match every preserved source field; a changed field
or outbox projection aborts the run.

Legacy admins map to disabled-login seller users by Telegram ID. Existing
orders retain their numeric IDs and all nullable/customer, delivery, message,
duplicate and timestamp fields. `pending` and `failed` deliveries create
corresponding outbox recovery rows; `sending` becomes `ambiguous` for manual
reconciliation. A failed row whose error starts with `Ambiguous ` is also
mapped to `ambiguous` in both the order and outbox; ordinary failed rows remain
manually retryable. `sent` is never replayed.

Run the importer again to verify idempotence: matching rows are reported as
already present and no additional rows or delivery jobs are created. Sequence
values are advanced automatically where PostgreSQL provides a sequence.

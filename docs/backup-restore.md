# Backup and restore runbook

These commands exercise the same PostgreSQL custom format used for a real operational backup. The guard allows only local disposable databases whose names end in `_test`; restore targets must end in `_restore_test`. URLs are read from environment variables; credentials are passed through `PGPASSWORD` for the child process and are never printed. PostgreSQL clients resolve from `PG_BIN` when set, then `PATH`.

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://<user>:<password>@localhost:<port>/<source_test_db>"
$env:PG_BIN = "<optional-postgresql-bin-directory>"
python -m alembic upgrade head
python -m scripts.seed_synthetic --reset
.\scripts\backup.ps1 -OutputPath artifacts\onlineshop.backup
```

On Linux/macOS, call the same cross platform entrypoint directly:

```bash
TEST_DATABASE_URL='postgresql+asyncpg://<user>:<password>@localhost:<port>/<source_test_db>' \
python -m scripts.pg_tools backup --output artifacts/onlineshop.backup
```

Restore is always into the separately named disposable database. The script creates it when absent and uses `pg_restore --clean --if-exists`:

```powershell
$env:RESTORE_DATABASE_URL = "postgresql+asyncpg://<user>:<password>@localhost:<port>/<restore_test_db>"
.\scripts\restore.ps1 -BackupPath artifacts\onlineshop.backup
python -m unittest tests.integration.test_restore -v
```

The portable Linux/macOS form is `RESTORE_DATABASE_URL='postgresql+asyncpg://<user>:<password>@localhost:<port>/<restore_test_db>' python -m scripts.pg_tools restore --backup artifacts/onlineshop.backup`.

The restore test compares synthetic IDs and counts and checks primary key, unique SKU, and non negative price constraints. For a production deployment, encrypt backups at rest, restrict access, retain according to the data policy, and test recovery from an off host copy with an approved change record.

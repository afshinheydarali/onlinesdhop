# Backup and restore runbook

These commands exercise the same PostgreSQL custom format used for a real operational backup, but the guard intentionally allows only the local disposable databases `onlineshop_portfolio_test` and `onlineshop_restore_test`. The source URL must be in `TEST_DATABASE_URL`; credentials are passed through `PGPASSWORD` for the child process and are never printed.

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://onlineshop_test:onlineshop-local-pg18-20260904@localhost:15432/onlineshop_portfolio_test"
python -m alembic upgrade head
python -m scripts.seed_synthetic --reset
.\scripts\backup.ps1 -OutputPath artifacts\onlineshop.backup
```

Restore is always into the separately named disposable database. The script creates it when absent and uses `pg_restore --clean --if-exists`:

```powershell
$restore = "postgresql+asyncpg://onlineshop_test:onlineshop-local-pg18-20260904@localhost:15432/onlineshop_restore_test"
.\scripts\restore.ps1 -BackupPath artifacts\onlineshop.backup -DatabaseUrl $restore
python -m unittest tests.integration.test_restore -v
```

The restore test compares synthetic IDs and counts and checks primary key, unique SKU, and non negative price constraints. For a production deployment, encrypt backups at rest, restrict access, retain according to the data policy, and test recovery from an off host copy with an approved change record.

# Import acceptance refinement after two rejected revisions

Supervisor review, 2026-09-06, importer commit 7000424. The latest claim of four passing tests and clean Ruff is rejected: independent execution produced one test error (duplicate source public IDs raised ValueError, not the intended destination IntegrityError) and twelve Ruff E701/E702 failures. Direct PostgreSQL query returned public.users_id_seq, public.orders_id_seq and public.outbox_id_seq; the reported absence of sequences was false. No schema change is needed for these tests.

## Package I1: prove sequences and rollback

Scope scripts/import_sqlite.py and tests/integration/test_import_sqlite.py, no backend schema edits. Require parsed explicit TEST_DATABASE_URL with localhost and database suffix _test. The existing migrated onlineshop_import_test database on port15432 is dedicated to this lane.

1. Repair the destination uniqueness fixture: after a successful fixture import, replace the SQLite source orders with ONE new unique source row whose id/draft_token are new but public_id collides with a destination row. Keep the source admin and valid duplicate references. Assert sqlalchemy.exc.IntegrityError during dry-run, then assert all destination rows and sequence states unchanged. Do not create duplicate public IDs inside the source; that tests an earlier validation boundary.
2. Snapshot last_value and is_called for users_id_seq, orders_id_seq and outbox_id_seq before an empty-destination dry-run and afterwards. Assert exact equality and no rows. Repeat against a nonempty destination with a new valid source row and verify rows and sequences remain unchanged.
3. After real import, create a new User, Order and Outbox using SQLAlchemy inserts with omitted primary keys and RETURNING id. Supply valid synthetic required fields and FKs. Assert each generated ID exceeds its table's imported maximum. This must exercise real generated IDs, not just call pg_get_serial_sequence or inspect the implementation.
4. Use normal readable multiline Python and import os directly. Run the targeted module and Ruff with the integration repository's configuration: `python -m ruff check --config D:/projects/onlineshop/pyproject.toml scripts/import_sqlite.py tests/integration/test_import_sqlite.py`. Both must return exit code zero. Commit and report actual results; no other import completeness claim.

## Package I2: preservation and uncertain delivery

Extend the fixture assertions to every preserved legacy field, nullable amount, delivered_at UTC, photo/text IDs, attempts, created_by mapping and source checksum. Add failed records with the stable Ambiguous prefix introduced by Plan003; these and interrupted sending map consistently to ambiguous recovery in Order and Outbox. Ordinary failed rows remain manually retryable. Document the intentional status mapping and compare the mapped expectation on repeat. Exercise invalid later-row rollback after earlier valid inserts and a repeat conflict after an earlier newly inserted row; assert no partial destination data. Run complete importer gates before integration.

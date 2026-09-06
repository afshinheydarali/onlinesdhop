"""enforce commerce stock and movement invariants"""

from alembic import op

revision = "0004_commerce_invariants"
down_revision = "0003_commerce_catalog_stock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
    for table, name, expression in [
        ("inventory_balances", "ck_inventory_reserved_lte_on_hand", "reserved <= on_hand"),
        ("reservations", "ck_reservations_status", "status IN ('reserved','released','consumed','expired')"),
        ("stock_movements", "ck_stock_movements_quantity_positive", "quantity > 0"),
        ("stock_movements", "ck_stock_movements_type", "movement_type IN ('reserve','release','consume','adjust')"),
    ]:
        op.execute(f"DO $$ BEGIN ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute(
        "DO $$ BEGIN ALTER TABLE stock_movements ADD CONSTRAINT "
        "uq_stock_movement_order_product_type UNIQUE (order_id, product_id, movement_type); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE stock_movements DROP CONSTRAINT IF EXISTS uq_stock_movement_order_product_type")
    op.drop_constraint("ck_stock_movements_type", "stock_movements", type_="check")
    op.drop_constraint("ck_stock_movements_quantity_positive", "stock_movements", type_="check")
    op.drop_constraint("ck_reservations_status", "reservations", type_="check")
    op.drop_constraint("ck_inventory_reserved_lte_on_hand", "inventory_balances", type_="check")
    op.drop_column("reservations", "expires_at")

"""enforce commerce stock and movement invariants"""

import sqlalchemy as sa
from alembic import op

revision = "0004_commerce_invariants"
down_revision = "0003_commerce_catalog_stock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_inventory_reserved_lte_on_hand", "inventory_balances", "reserved <= on_hand")
    op.create_check_constraint("ck_reservations_status", "reservations", "status IN ('reserved','released','consumed','expired')")
    op.create_check_constraint("ck_stock_movements_quantity_positive", "stock_movements", "quantity > 0")
    op.create_check_constraint("ck_stock_movements_type", "stock_movements", "movement_type IN ('reserve','release','consume','adjust')")
    op.create_unique_constraint("uq_stock_movement_order_product_type", "stock_movements", ["order_id", "product_id", "movement_type"])


def downgrade() -> None:
    op.drop_constraint("uq_stock_movement_order_product_type", "stock_movements", type_="unique")
    op.drop_constraint("ck_stock_movements_type", "stock_movements", type_="check")
    op.drop_constraint("ck_stock_movements_quantity_positive", "stock_movements", type_="check")
    op.drop_constraint("ck_reservations_status", "reservations", type_="check")
    op.drop_constraint("ck_inventory_reserved_lte_on_hand", "inventory_balances", type_="check")
    op.drop_column("reservations", "expires_at")

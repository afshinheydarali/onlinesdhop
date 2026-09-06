"""allow legacy test/import cleanup to remove parent orders"""

from alembic import op

revision = "0005_commerce_cascade_cleanup"
down_revision = "0004_commerce_invariants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, name, ref in [
        ("order_items", "order_items_order_id_fkey", "orders(id)"),
        ("reservations", "reservations_order_id_fkey", "orders(id)"),
        ("stock_movements", "stock_movements_order_id_fkey", "orders(id)"),
    ]:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (order_id) REFERENCES {ref} ON DELETE CASCADE")


def downgrade() -> None:
    for table, name, ref in [
        ("order_items", "order_items_order_id_fkey", "orders(id)"),
        ("reservations", "reservations_order_id_fkey", "orders(id)"),
        ("stock_movements", "stock_movements_order_id_fkey", "orders(id)"),
    ]:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (order_id) REFERENCES {ref}")

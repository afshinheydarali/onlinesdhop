"""catalog, immutable order items, and stock reservations"""

import sqlalchemy as sa
from alembic import op

revision = "0003_commerce_catalog_stock"
down_revision = "0002_api_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("currency", sa.String(3), nullable=True))
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("sku", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("unit_price", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="IRR"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("unit_price >= 0", name="ck_products_price_nonnegative"),
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("sku_snapshot", sa.String(80), nullable=False),
        sa.Column("name_snapshot", sa.String(200), nullable=False),
        sa.Column("unit_price_snapshot", sa.BigInteger(), nullable=False),
        sa.Column("currency_snapshot", sa.String(3), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price_snapshot >= 0", name="ck_order_items_price_nonnegative"),
        sa.CheckConstraint("line_total >= 0", name="ck_order_items_total_nonnegative"),
    )
    op.create_table(
        "inventory_balances",
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), primary_key=True),
        sa.Column("on_hand", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("on_hand >= 0", name="ck_inventory_on_hand_nonnegative"),
        sa.CheckConstraint("reserved >= 0", name="ck_inventory_reserved_nonnegative"),
    )
    op.create_table(
        "reservations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="reserved"),
        sa.UniqueConstraint("order_id", "product_id", name="uq_reservation_order_product"),
        sa.CheckConstraint("quantity > 0", name="ck_reservations_quantity_positive"),
    )
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id")),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("movement_type", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("stock_movements")
    op.drop_table("reservations")
    op.drop_table("inventory_balances")
    op.drop_table("order_items")
    op.drop_table("products")
    op.drop_column("orders", "currency")

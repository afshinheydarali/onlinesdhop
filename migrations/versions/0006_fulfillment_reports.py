"""separate fulfillment/payment state, transition audit and payment reconciliation"""

import sqlalchemy as sa
from alembic import op

revision = "0006_fulfillment_reports"
down_revision = "0005_commerce_cascade_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("fulfillment_status", sa.String(20), nullable=False, server_default="draft"))
    op.add_column("orders", sa.Column("payment_status", sa.String(20), nullable=False, server_default="pending"))
    op.add_column("orders", sa.Column("reconciliation_required", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_orders_fulfillment_status",
        "orders",
        "fulfillment_status IN ('draft','confirmed','packing','shipped','delivered','cancelled','expired')",
    )
    op.create_check_constraint("ck_orders_payment_status", "orders", "payment_status IN ('pending','paid','failed','refunded')")
    op.create_table(
        "fulfillment_transitions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_role", sa.String(20), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=False),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_fulfillment_transitions_order_time", "fulfillment_transitions", ["order_id", "transitioned_at", "id"])
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False, server_default="fake"),
        sa.Column("provider_transaction_id", sa.String(200), nullable=True),
        sa.Column("provider_event_id", sa.String(200), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "provider_event_id", name="uq_payment_provider_event"),
        sa.UniqueConstraint("provider", "provider_transaction_id", name="uq_payment_provider_transaction"),
    )
    op.create_index("ix_payment_attempts_order", "payment_attempts", ["order_id", "created_at"])
    op.create_index("ix_orders_fulfillment_status_created", "orders", ["fulfillment_status", "created_at", "id"])
    op.create_index("ix_orders_payment_report", "orders", ["payment_status", "reconciliation_required", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_orders_payment_report", table_name="orders")
    op.drop_index("ix_orders_fulfillment_status_created", table_name="orders")
    op.drop_index("ix_payment_attempts_order", table_name="payment_attempts")
    op.drop_table("payment_attempts")
    op.drop_index("ix_fulfillment_transitions_order_time", table_name="fulfillment_transitions")
    op.drop_table("fulfillment_transitions")
    op.drop_constraint("ck_orders_payment_status", "orders", type_="check")
    op.drop_constraint("ck_orders_fulfillment_status", "orders", type_="check")
    op.drop_column("orders", "expired_at")
    op.drop_column("orders", "reconciliation_required")
    op.drop_column("orders", "payment_status")
    op.drop_column("orders", "fulfillment_status")

"""Add payment verification receipts and commerce lifecycle fields."""

import sqlalchemy as sa
from alembic import op

revision = "0006_payment_delivery"
down_revision = "0005_commerce_cascade_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("payment_status", sa.String(24), nullable=False, server_default="pending"))
    op.add_column("orders", sa.Column("fulfillment_status", sa.String(24), nullable=False, server_default="confirmed"))
    op.add_column("orders", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("reconciliation_required", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint(
        "ck_orders_payment_status", "orders",
        "payment_status IN ('pending','paid','failed','refunded')",
    )
    op.create_check_constraint(
        "ck_orders_fulfillment_status", "orders",
        "fulfillment_status IN ('draft','confirmed','packing','shipped','delivered','cancelled','expired')",
    )
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False, server_default="fake"),
        sa.Column("provider_transaction_id", sa.String(160), nullable=False, unique=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("reconciliation_reason", sa.String(240)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount >= 0", name="ck_payment_attempt_amount_nonnegative"),
        sa.CheckConstraint("status IN ('pending','paid','failed','reconciliation')", name="ck_payment_attempt_status"),
    )
    op.create_index("ix_payment_attempts_order_id", "payment_attempts", ["order_id"])
    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider_event_id", sa.String(160), nullable=False, unique=True),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("provider_transaction_id", sa.String(160), nullable=False),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id")),
        sa.Column("payment_attempt_id", sa.BigInteger(), sa.ForeignKey("payment_attempts.id")),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_payment_events_provider_transaction_id", "payment_events", ["provider_transaction_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_events_provider_transaction_id", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index("ix_payment_attempts_order_id", table_name="payment_attempts")
    op.drop_table("payment_attempts")
    op.drop_constraint("ck_orders_fulfillment_status", "orders", type_="check")
    op.drop_constraint("ck_orders_payment_status", "orders", type_="check")
    op.drop_column("orders", "expires_at")
    op.drop_column("orders", "expired_at")
    op.drop_column("orders", "reconciliation_required")
    op.drop_column("orders", "fulfillment_status")
    op.drop_column("orders", "payment_status")

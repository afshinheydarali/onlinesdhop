"""persist reconciliation work created by fulfillment/payment races"""

import sqlalchemy as sa
from alembic import op

revision = "0007_payment_reconciliation"
down_revision = "0006_fulfillment_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_reconciliations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_attempt_id", sa.BigInteger(), sa.ForeignKey("payment_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('open','resolved')", name="ck_payment_reconciliation_status"),
        sa.UniqueConstraint("order_id", "kind", name="uq_payment_reconciliation_order_kind"),
    )
    op.create_index("ix_payment_reconciliations_order", "payment_reconciliations", ["order_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_payment_reconciliations_order", table_name="payment_reconciliations")
    op.drop_table("payment_reconciliations")

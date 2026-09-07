"""persist signed provider callback receipts and replay fingerprints"""

import sqlalchemy as sa
from alembic import op

revision = "0008_payment_delivery"
down_revision = "0007_payment_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PaymentAttempt and PaymentReconciliation are created by 0006/0007. The
    # signed callback needs only an immutable receipt for event replay fencing.
    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False, server_default="fake"),
        sa.Column("provider_event_id", sa.String(200), nullable=False),
        sa.Column("provider_transaction_id", sa.String(200), nullable=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payment_attempt_id", sa.BigInteger(), sa.ForeignKey("payment_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "provider_event_id", name="uq_payment_event_provider_event"),
    )
    op.create_index(
        "ix_payment_events_provider_transaction",
        "payment_events",
        ["provider", "provider_transaction_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_payment_events_provider_transaction", table_name="payment_events")
    op.drop_table("payment_events")

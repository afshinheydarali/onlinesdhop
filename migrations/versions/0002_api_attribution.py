"""Allow API-created orders to retain their authenticated creator."""
from alembic import op
import sqlalchemy as sa

revision = "0002_api_attribution"
down_revision = "0001_backend_foundation"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.alter_column("orders", "admin_telegram_id", nullable=True)
    op.add_column("orders", sa.Column("created_by_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True))

def downgrade() -> None:
    op.drop_column("orders", "created_by_id")
    op.alter_column("orders", "admin_telegram_id", nullable=False)

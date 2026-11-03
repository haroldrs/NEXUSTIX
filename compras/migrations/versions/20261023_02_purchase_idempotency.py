"""Persist idempotency keys and responses for individual purchases."""
from alembic import op
import sqlalchemy as sa

revision = "20261023_02"
down_revision = "20261022_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "purchase_idempotency",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("auth_subject", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("operacion_id", sa.String(64), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("response_status IS NULL OR (response_status >= 100 AND response_status <= 599)", name="ck_purchase_idempotency_status"),
        sa.CheckConstraint("state IN ('processing', 'completed')", name="ck_purchase_idempotency_state"),
    )
    op.create_index("ix_purchase_idempotency_auth_subject", "purchase_idempotency", ["auth_subject"])


def downgrade():
    bind = op.get_bind()
    used_keys = bind.execute(sa.text("SELECT count(*) FROM purchase_idempotency")).scalar_one()
    if used_keys:
        raise RuntimeError("Downgrade detenido: existen resultados idempotentes que se perderían.")
    op.drop_index("ix_purchase_idempotency_auth_subject", table_name="purchase_idempotency")
    op.drop_table("purchase_idempotency")

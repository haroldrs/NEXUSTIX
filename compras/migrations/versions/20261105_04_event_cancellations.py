"""Estado durable de las cancelaciones de eventos y sus compensaciones."""
from alembic import op
import sqlalchemy as sa

revision = "20261105_04"
down_revision = "20261102_03"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cancelaciones_evento",
        sa.Column("evento_id", sa.Integer(), primary_key=True),
        sa.Column("estado", sa.String(20), nullable=False, server_default="pendiente"),
        sa.Column("motivo", sa.String(300)),
        sa.Column("solicitada_por", sa.String(128), nullable=False),
        sa.Column("total_ordenes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errores", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("creado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_cancelaciones_evento_estado", "cancelaciones_evento", ["estado"])


def downgrade():
    raise RuntimeError("Downgrade bloqueado: conservar evidencia de cancelaciones.")

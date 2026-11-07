"""Reembolsos por evento e intención de cancelación recuperable.

Preparada para ejecutarse con el mecanismo de migraciones existente. No reescribe
las migraciones ya aplicadas ni recalcula importes históricos desconocidos.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261105_06"
down_revision = "20261105_05"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cancelaciones_evento", sa.Column("ventas_cerradas_en", sa.DateTime(timezone=True)))
    op.add_column("cancelaciones_evento", sa.Column("ultimo_error", sa.String(300)))
    op.add_column("cancelaciones_evento", sa.Column("legacy_pendientes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("pagos_simulados", sa.Column("reembolsado_centimos", sa.Integer(), nullable=False, server_default="0"))
    # Los reembolsos completos anteriores sí tienen importe verificable en Pago.
    op.execute("UPDATE pagos_simulados SET reembolsado_centimos = total_centimos WHERE estado = 'reembolsado'")
    op.create_table(
        "reembolsos_evento",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("orden_id", sa.String(32), sa.ForeignKey("ordenes.id"), nullable=False),
        sa.Column("evento_id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="pendiente"),
        sa.Column("total_centimos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("motivo", sa.String(300), nullable=False),
        sa.Column("solicitado_por", sa.String(128), nullable=False),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ultimo_error", sa.String(300)),
        sa.Column("creado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("orden_id", "evento_id", name="uq_reembolso_orden_evento"),
        sa.CheckConstraint("total_centimos >= 0", name="ck_reembolso_importe"),
        sa.CheckConstraint("estado IN ('pendiente','procesando','completado','error_recuperable')", name="ck_reembolso_estado"),
    )
    for column in ("orden_id", "evento_id", "estado"):
        op.create_index("ix_reembolsos_evento_" + column, "reembolsos_evento", [column])


def downgrade():
    raise RuntimeError("Downgrade bloqueado: conservar pagos, compensaciones y auditoría")

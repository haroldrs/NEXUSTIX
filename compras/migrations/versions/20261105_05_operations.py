"""Capturas explícitas, incidentes observados y evidencias de demostración."""
from alembic import op
import sqlalchemy as sa

revision = "20261105_05"
down_revision = "20261105_04"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("evidencias_operacion",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("escenario", sa.String(1), nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("documento", sa.JSON(), nullable=False))
    op.create_index("ix_evidencias_operacion_escenario", "evidencias_operacion", ["escenario"])
    op.create_table("incidentes_operacion",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("componente", sa.String(48), nullable=False),
        sa.Column("detectado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recuperado_en", sa.DateTime(timezone=True)),
        sa.Column("documento", sa.JSON(), nullable=False))
    op.create_index("ix_incidentes_operacion_componente", "incidentes_operacion", ["componente"])
    op.create_table("observaciones_componentes",
        sa.Column("componente", sa.String(48), primary_key=True),
        sa.Column("capturado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("incidente_id", sa.String(32)),
        sa.Column("documento", sa.JSON(), nullable=False))


def downgrade():
    raise RuntimeError("Downgrade bloqueado: conservar evidencias e historial de incidentes.")

"""Idempotent legacy baseline for existing installs and empty databases."""
from alembic import op

revision = "20261022_00"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS compras (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER NOT NULL,
        evento_id INTEGER NOT NULL,
        cantidad INTEGER NOT NULL,
        estado VARCHAR DEFAULT 'confirmada',
        instancia VARCHAR,
        creado_en TIMESTAMPTZ DEFAULT now(),
        operacion_id VARCHAR(64),
        CONSTRAINT ck_compras_cantidad_positiva CHECK (cantidad > 0)
    )
    """)
    op.execute("ALTER TABLE compras ADD COLUMN IF NOT EXISTS operacion_id VARCHAR(64)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_compras_operacion_id ON compras (operacion_id) WHERE operacion_id IS NOT NULL")
    op.execute("""
    CREATE TABLE IF NOT EXISTS journal_compras (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
        reloj_t INTEGER NOT NULL,
        instancia VARCHAR(16) NOT NULL,
        tipo VARCHAR(40) NOT NULL,
        mensaje VARCHAR NOT NULL,
        detalles JSON NOT NULL DEFAULT '{}'::json
    )
    """)


def downgrade():
    # Baseline is deliberately non-destructive: it may have described pre-existing data.
    pass

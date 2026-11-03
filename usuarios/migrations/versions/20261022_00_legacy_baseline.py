"""Idempotent legacy baseline for existing installs and empty databases."""
from alembic import op

revision = "20261022_00"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        nombre VARCHAR NOT NULL,
        email VARCHAR NOT NULL UNIQUE,
        estado VARCHAR
    )
    """)


def downgrade():
    # Baseline is deliberately non-destructive: it may have described pre-existing data.
    pass

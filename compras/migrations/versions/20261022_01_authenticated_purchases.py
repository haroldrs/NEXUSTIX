"""Add verified identity subject to future purchases; legacy rows remain NULL."""
from alembic import op
import sqlalchemy as sa

revision = "20261022_01"
down_revision = "20261022_00"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("compras", sa.Column("auth_subject", sa.String(128), nullable=True))
    op.create_index(
        "ix_compras_auth_subject_created_en",
        "compras",
        ["auth_subject", "creado_en"],
        postgresql_where=sa.text("auth_subject IS NOT NULL"),
    )


def downgrade():
    bind = op.get_bind()
    authenticated_purchases = bind.execute(sa.text("SELECT count(*) FROM compras WHERE auth_subject IS NOT NULL")).scalar_one()
    if authenticated_purchases:
        raise RuntimeError("Downgrade detenido: hay compras asociadas a sujetos autenticados.")
    op.drop_index("ix_compras_auth_subject_created_en", table_name="compras")
    op.drop_column("compras", "auth_subject")

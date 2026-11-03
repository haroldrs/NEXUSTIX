"""Autorizar organizadores; no convierte cuentas existentes."""
from alembic import op

revision = "20261102_02"
down_revision = "20261022_01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("usuarios") as batch:
        batch.drop_constraint("ck_usuarios_role", type_="check")
        batch.create_check_constraint("ck_usuarios_role", "role IN ('comprador', 'administrador', 'organizador')")


def downgrade():
    raise RuntimeError("No se degradan roles automáticamente")

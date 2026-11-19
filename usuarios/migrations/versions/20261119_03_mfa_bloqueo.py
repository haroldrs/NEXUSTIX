"""MFA (TOTP) opcional y bloqueo temporal por intentos fallidos.

Solo agrega columnas con valor por defecto: las cuentas existentes siguen
entrando con su contraseña y sin MFA hasta que lo activen.
"""
import sqlalchemy as sa
from alembic import op

revision = "20261119_03"
down_revision = "20261102_02"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("usuarios") as batch:
        batch.add_column(sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("mfa_secret", sa.String(64), nullable=True))
        batch.add_column(sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))


def downgrade():
    raise RuntimeError("No se eliminan datos de seguridad automáticamente")

"""Add authentication and revocable refresh-session storage."""
from alembic import op
import sqlalchemy as sa

revision = "20261022_01"
down_revision = "20261022_00"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("usuarios", sa.Column("password_hash", sa.String(512), nullable=True))
    op.add_column("usuarios", sa.Column("role", sa.String(24), nullable=False, server_default="comprador"))
    op.add_column("usuarios", sa.Column("auth_status", sa.String(32), nullable=False, server_default="legacy_unclaimed"))
    op.create_check_constraint("ck_usuarios_role", "usuarios", "role IN ('comprador', 'administrador')")
    op.create_check_constraint("ck_usuarios_auth_status", "usuarios", "auth_status IN ('legacy_unclaimed', 'active', 'disabled')")
    op.create_check_constraint("ck_usuarios_active_has_password", "usuarios", "auth_status <> 'active' OR password_hash IS NOT NULL")
    # Preflight first: SELECT lower(trim(email)), count(*) FROM usuarios GROUP BY lower(trim(email)) HAVING count(*) > 1.
    op.create_index("uq_usuarios_email_lower", "usuarios", [sa.text("lower(trim(email))")], unique=True)
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("family_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("usuarios.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_hash", sa.String(64), nullable=True),
    )
    op.create_index("ix_refresh_sessions_family_id", "refresh_sessions", ["family_id"])
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])


def downgrade():
    bind = op.get_bind()
    live_sessions = bind.execute(sa.text("SELECT count(*) FROM refresh_sessions")).scalar_one()
    authenticated_users = bind.execute(sa.text("SELECT count(*) FROM usuarios WHERE password_hash IS NOT NULL OR auth_status <> 'legacy_unclaimed' OR role <> 'comprador'")).scalar_one()
    if live_sessions or authenticated_users:
        raise RuntimeError("Downgrade detenido: ya existen sesiones/cuentas autenticadas; no se borrará material de autenticación.")
    op.drop_index("ix_refresh_sessions_user_id", table_name="refresh_sessions")
    op.drop_index("ix_refresh_sessions_family_id", table_name="refresh_sessions")
    op.drop_table("refresh_sessions")
    op.drop_index("uq_usuarios_email_lower", table_name="usuarios")
    op.drop_constraint("ck_usuarios_auth_status", "usuarios", type_="check")
    op.drop_constraint("ck_usuarios_active_has_password", "usuarios", type_="check")
    op.drop_constraint("ck_usuarios_role", "usuarios", type_="check")
    op.drop_column("usuarios", "auth_status")
    op.drop_column("usuarios", "role")
    op.drop_column("usuarios", "password_hash")

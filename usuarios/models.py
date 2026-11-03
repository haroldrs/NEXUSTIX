from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.sql import func

from database import Base


class Usuario(Base):
    __tablename__ = "usuarios"
    __table_args__ = (
        CheckConstraint("role IN ('comprador', 'administrador', 'organizador')", name="ck_usuarios_role"),
        CheckConstraint("auth_status IN ('legacy_unclaimed', 'active', 'disabled')", name="ck_usuarios_auth_status"),
        CheckConstraint("auth_status <> 'active' OR password_hash IS NOT NULL", name="ck_usuarios_active_has_password"),
        Index("uq_usuarios_email_lower", text("lower(trim(email))"), unique=True),
    )

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    estado = Column(String, default="activo")
    password_hash = Column(String(512), nullable=True)
    role = Column(String(24), nullable=False, default="comprador", server_default="comprador")
    auth_status = Column(String(32), nullable=False, default="legacy_unclaimed", server_default="legacy_unclaimed")


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id = Column(String(64), primary_key=True)
    family_id = Column(String(64), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("usuarios.id", ondelete="RESTRICT"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    replaced_by_hash = Column(String(64), nullable=True)

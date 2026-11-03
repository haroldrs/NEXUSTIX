"""Pedidos, pagos simulados, entradas y outbox; solo tablas nuevas."""
from alembic import op
from sqlalchemy import Column, String, Integer, DateTime, JSON, ForeignKey, CheckConstraint, UniqueConstraint, Table, MetaData
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
revision = "20261102_03"
down_revision = "20261023_02"
branch_labels = None
depends_on = None
Base = declarative_base(metadata=MetaData())
# Referencia únicamente para resolver la FK a la tabla existente.
Table("compras", Base.metadata, Column("id", Integer, primary_key=True))

class Orden(Base):
    __tablename__ = "ordenes"
    __table_args__ = (CheckConstraint("estado IN ('reservando','reservada','pagada','emitiendo','completada','cancelando','cancelada','reembolsando','reembolsada')", name="ck_orden_estado"),)
    id = Column(String(32), primary_key=True)
    clave = Column(String(128), nullable=False, unique=True)
    request_hash = Column(String(64), nullable=False)
    auth_subject = Column(String(128), nullable=False, index=True)
    usuario_id = Column(Integer, nullable=False)
    estado = Column(String(24), nullable=False, default="reservando", index=True)
    items = Column(JSON, nullable=False)
    total_centimos = Column(Integer, nullable=False, default=0)
    expira_en = Column(DateTime(timezone=True), nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    motivo = Column(String(120), nullable=True)


class Pago(Base):
    __tablename__ = "pagos_simulados"
    orden_id = Column(String(32), ForeignKey("ordenes.id"), primary_key=True)
    estado = Column(String(24), nullable=False)
    total_centimos = Column(Integer, nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Entrada(Base):
    __tablename__ = "entradas"
    __table_args__ = (UniqueConstraint("orden_id", "item", "numero", name="uq_entrada_unidad"),)
    id = Column(String(48), primary_key=True)
    orden_id = Column(String(32), ForeignKey("ordenes.id"), nullable=False, index=True)
    compra_id = Column(Integer, ForeignKey("compras.id"), nullable=False)
    evento_id = Column(Integer, nullable=False)
    item = Column(Integer, nullable=False)
    numero = Column(Integer, nullable=False)
    asiento = Column(String(40), nullable=True)
    estado = Column(String(20), nullable=False, default="emitida")
    usado_en = Column(DateTime(timezone=True), nullable=True)
    usado_por = Column(String(128), nullable=True)


class Outbox(Base):
    __tablename__ = "outbox"
    id = Column(Integer, primary_key=True)
    orden_id = Column(String(32), ForeignKey("ordenes.id"), nullable=False, index=True)
    tipo = Column(String(40), nullable=False)
    dedupe = Column(String(100), nullable=False, unique=True)
    datos = Column(JSON, nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    publicado_en = Column(DateTime(timezone=True), nullable=True)


class Notificacion(Base):
    __tablename__ = "notificaciones"
    evento_id = Column(Integer, ForeignKey("outbox.id"), primary_key=True)
    auth_subject = Column(String(128), nullable=False, index=True)
    mensaje = Column(String(200), nullable=False)


def upgrade():
    for name in ("ordenes", "pagos_simulados", "entradas", "outbox", "notificaciones"):
        Base.metadata.tables[name].create(op.get_bind())


def downgrade():
    raise RuntimeError("Downgrade bloqueado: conservar pedidos, pagos y entradas; restaurar en un entorno aislado.")

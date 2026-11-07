from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, JSON, text, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func

from database import Base


class Compra(Base):
    __tablename__ = "compras"
    __table_args__ = (
        CheckConstraint("cantidad > 0", name="ck_compras_cantidad_positiva"),
        Index(
            "ix_compras_auth_subject_created_en",
            "auth_subject",
            "creado_en",
            postgresql_where=text("auth_subject IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, nullable=False)
    evento_id = Column(Integer, nullable=False)
    cantidad = Column(Integer, nullable=False)
    estado = Column(String, default="confirmada")
    instancia = Column(String, nullable=True)
    # Identificador que vincula la compra con la reserva en el servicio Eventos.
    # Nullable para conservar compras antiguas creadas antes de esta mejora.
    operacion_id = Column(String(64), nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())
    # Sujeto firmado de la identidad autenticada. NULL identifica compras legacy
    # cuya autoría no se puede probar desde el flujo anterior.
    auth_subject = Column(String(128), nullable=True)


class RegistroJournal(Base):
    """Bitácora persistente compartida por las réplicas de Compras.

    No reemplaza el WAL de PostgreSQL; registra eventos funcionales y de recuperación.
    """
    __tablename__ = "journal_compras"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reloj_t = Column(Integer, nullable=False, default=0)
    instancia = Column(String(16), nullable=False)
    tipo = Column(String(40), nullable=False)
    mensaje = Column(String, nullable=False)
    detalles = Column(JSON, nullable=False, default=dict)


class IdempotencyRequest(Base):
    """Durable response for one globally unique client purchase key."""
    __tablename__ = "purchase_idempotency"
    __table_args__ = (
        CheckConstraint("response_status IS NULL OR (response_status >= 100 AND response_status <= 599)", name="ck_purchase_idempotency_status"),
        CheckConstraint("state IN ('processing', 'completed')", name="ck_purchase_idempotency_state"),
    )

    idempotency_key = Column(String(128), primary_key=True)
    auth_subject = Column(String(128), nullable=False, index=True)
    request_hash = Column(String(64), nullable=False)
    operacion_id = Column(String(64), nullable=False, unique=True)
    state = Column(String(16), nullable=False, default="processing")
    response_status = Column(Integer, nullable=True)
    response_body = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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
    reembolsado_centimos = Column(Integer, nullable=False, default=0, server_default="0")
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


class CancelacionEvento(Base):
    __tablename__ = "cancelaciones_evento"
    evento_id = Column(Integer, primary_key=True)
    estado = Column(String(20), nullable=False, default="pendiente", index=True)
    motivo = Column(String(300), nullable=True)
    solicitada_por = Column(String(128), nullable=False)
    total_ordenes = Column(Integer, nullable=False, default=0)
    completadas = Column(Integer, nullable=False, default=0)
    errores = Column(Integer, nullable=False, default=0)
    ventas_cerradas_en = Column(DateTime(timezone=True), nullable=True)
    ultimo_error = Column(String(300), nullable=True)
    legacy_pendientes = Column(Integer, nullable=False, default=0, server_default="0")
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actualizado_en = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ReembolsoEvento(Base):
    """Un efecto por pedido/evento, compartido por ambas réplicas de Compras."""
    __tablename__ = "reembolsos_evento"
    __table_args__ = (
        UniqueConstraint("orden_id", "evento_id", name="uq_reembolso_orden_evento"),
        CheckConstraint("total_centimos >= 0", name="ck_reembolso_importe"),
        CheckConstraint("estado IN ('pendiente','procesando','completado','error_recuperable')", name="ck_reembolso_estado"),
    )
    id = Column(String(32), primary_key=True)
    orden_id = Column(String(32), ForeignKey("ordenes.id"), nullable=False, index=True)
    evento_id = Column(Integer, nullable=False, index=True)
    tipo = Column(String(30), nullable=False)
    estado = Column(String(20), nullable=False, default="pendiente", index=True)
    total_centimos = Column(Integer, nullable=False, default=0)
    motivo = Column(String(300), nullable=False)
    solicitado_por = Column(String(128), nullable=False)
    intentos = Column(Integer, nullable=False, default=0)
    ultimo_error = Column(String(300), nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actualizado_en = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

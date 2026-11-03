from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, JSON, Boolean, UniqueConstraint
from sqlalchemy.sql import func

from database import Base


class Evento(Base):
    __tablename__ = "eventos"
    __table_args__ = (
        CheckConstraint("stock >= 0", name="ck_eventos_stock_no_negativo"),
        CheckConstraint("stock_inicial IS NULL OR stock_inicial >= 0", name="ck_eventos_stock_inicial_no_negativo"),
    )

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    stock = Column(Integer, nullable=False, default=0)
    # Aforo original del evento, base de la ecuación de auditoría
    # stock_inicial == stock + reservadas + confirmadas. NULL en filas creadas
    # sin este dato (la auditoría las marca como no verificables).
    stock_inicial = Column(Integer, nullable=True)
    descripcion = Column(String, nullable=False, default="", server_default="")
    lugar = Column(String(200), nullable=False, default="", server_default="")
    fecha = Column(DateTime(timezone=True), nullable=True)
    precio_centimos = Column(Integer, nullable=False, default=0, server_default="0")
    organizador = Column(String(128), nullable=True)
    activo = Column(Boolean, nullable=False, default=True, server_default="true")


class ReservaStock(Base):
    """Registro idempotente de reservas y compensaciones de stock.

    Cada operación tiene un identificador único para impedir que un reintento
    descuente o libere dos veces las mismas entradas.
    """

    __tablename__ = "reservas_stock"
    __table_args__ = (
        CheckConstraint("cantidad > 0", name="ck_reservas_stock_cantidad_positiva"),
        CheckConstraint(
            "estado IN ('reservada', 'confirmada', 'liberada')",
            name="ck_reservas_stock_estado_valido",
        ),
    )

    operacion_id = Column(String(64), primary_key=True)
    evento_id = Column(Integer, ForeignKey("eventos.id"), nullable=False, index=True)
    cantidad = Column(Integer, nullable=False)
    estado = Column(String(20), nullable=False, default="reservada", index=True)
    asientos = Column(JSON, nullable=True)
    total_centimos = Column(Integer, nullable=False, default=0, server_default="0")
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actualizado_en = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Asiento(Base):
    __tablename__ = "asientos"
    __table_args__ = (UniqueConstraint("evento_id", "codigo", name="uq_asiento_evento_codigo"),
                      CheckConstraint("precio_centimos >= 0", name="ck_asiento_precio"))
    id = Column(Integer, primary_key=True)
    evento_id = Column(Integer, ForeignKey("eventos.id"), nullable=False, index=True)
    codigo = Column(String(40), nullable=False)
    zona = Column(String(80), nullable=False)
    precio_centimos = Column(Integer, nullable=False)
    operacion_id = Column(String(64), nullable=True, index=True)


class OperacionCerrada(Base):
    """Impide que una reserva tardía reviva una operación ya compensada."""
    __tablename__ = "operaciones_cerradas"
    operacion_id = Column(String(64), primary_key=True)
    evento_id = Column(Integer, nullable=False)  # Puede cerrar un evento aún inexistente.


class AuditoriaCatalogo(Base):
    __tablename__ = "auditoria_catalogo"
    id = Column(Integer, primary_key=True)
    evento_id = Column(Integer, ForeignKey("eventos.id"), nullable=False)
    actor = Column(String(128), nullable=False)
    accion = Column(String(24), nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

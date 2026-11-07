"""Evidencias administrativas; se escriben solo al solicitar una captura/importación."""
from sqlalchemy import Column, DateTime, JSON, String
from sqlalchemy.sql import func

from database import Base


class EvidenciaOperacion(Base):
    __tablename__ = "evidencias_operacion"
    id = Column(String(32), primary_key=True)
    escenario = Column(String(1), nullable=False, index=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    documento = Column(JSON, nullable=False)


class IncidenteOperacion(Base):
    __tablename__ = "incidentes_operacion"
    id = Column(String(32), primary_key=True)
    componente = Column(String(48), nullable=False, index=True)
    detectado_en = Column(DateTime(timezone=True), nullable=False)
    recuperado_en = Column(DateTime(timezone=True), nullable=True)
    documento = Column(JSON, nullable=False)


class ObservacionComponente(Base):
    __tablename__ = "observaciones_componentes"
    componente = Column(String(48), primary_key=True)
    capturado_en = Column(DateTime(timezone=True), nullable=False)
    estado = Column(String(20), nullable=False)
    incidente_id = Column(String(32), nullable=True)
    documento = Column(JSON, nullable=False)

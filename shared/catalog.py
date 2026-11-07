"""Catálogo administrable, asientos e inventario reutilizado por el checkout."""
from datetime import datetime
from typing import List
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text, func
from shared.auth_tokens import authenticated_claims, require_service_token


class SeatSpec(BaseModel):
    codigo: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    zona: str = Field(min_length=1, max_length=80)
    precio_centimos: int = Field(ge=0, le=1000000)


class EventSpec(BaseModel):
    model_config = {"extra": "forbid"}
    nombre: str = Field(min_length=1, max_length=200)
    descripcion: str = Field(default="", max_length=4000)
    lugar: str = Field(default="", max_length=200)
    categoria: str = Field(default="", max_length=80)
    imagen_url: str = Field(default="", max_length=2000)
    fecha: datetime
    capacidad: int = Field(ge=1, le=100000)
    precio_centimos: int = Field(ge=0, le=1000000)
    asientos: List[SeatSpec] = Field(default_factory=list, max_length=10000)

    @field_validator("imagen_url")
    @classmethod
    def image_url(cls, value):
        from urllib.parse import urlsplit
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("La imagen debe ser una URL HTTP(S) sin credenciales")
        return value


class EventEdit(BaseModel):
    model_config = {"extra": "forbid"}
    nombre: str = Field(min_length=1, max_length=200)
    descripcion: str = Field(default="", max_length=4000)
    lugar: str = Field(default="", max_length=200)
    fecha: datetime
    activo: bool = True
    precio_centimos: int | None = Field(default=None, ge=0, le=1000000)
    categoria: str | None = Field(default=None, max_length=80)
    imagen_url: str | None = Field(default=None, max_length=2000)

    @field_validator("imagen_url")
    @classmethod
    def image_url(cls, value):
        return EventSpec.image_url(value) if value is not None else value


def actor(authorization: str | None = Header(default=None)):
    return authenticated_claims(authorization)


def owner(event, claims):
    if claims["role"] not in {"administrador", "organizador"}:
        raise HTTPException(403, "Se requiere organizador o administrador")
    if claims["role"] != "administrador" and event.organizador != f"{claims['iss']}|{claims['sub']}":
        raise HTTPException(403, "No administras este evento")


def serialize(event, precio_asiento=None):
    result = {k: (getattr(event, k).isoformat() if k in {"fecha", "cancelado_en"} and getattr(event, k) else getattr(event, k))
            for k in ("id", "nombre", "stock", "stock_inicial", "descripcion", "lugar", "categoria", "imagen_url", "fecha", "precio_centimos", "activo", "cancelado_en", "motivo_cancelacion")}
    result["precio_desde_centimos"] = precio_asiento if precio_asiento is not None else event.precio_centimos
    return result


def migrate(connection):
    # Aditivo bajo el advisory lock de Eventos. Nunca recalcula stock existente.
    for column in ("descripcion TEXT NOT NULL DEFAULT ''", "lugar VARCHAR(200) NOT NULL DEFAULT ''",
                   "fecha TIMESTAMPTZ", "precio_centimos INTEGER NOT NULL DEFAULT 0",
                   "organizador VARCHAR(128)", "activo BOOLEAN NOT NULL DEFAULT true"):
        connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS " + column))
    connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS cancelado_en TIMESTAMPTZ"))
    connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS motivo_cancelacion VARCHAR(300)"))
    connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS cancelado_por VARCHAR(128)"))
    connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS categoria VARCHAR(80) NOT NULL DEFAULT ''"))
    connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS imagen_url VARCHAR(2000) NOT NULL DEFAULT ''"))
    # Preservar la auditoría incluso cuando se elimina un evento sin reservas.
    connection.execute(text("ALTER TABLE auditoria_catalogo DROP CONSTRAINT IF EXISTS auditoria_catalogo_evento_id_fkey"))
    connection.execute(text("ALTER TABLE reservas_stock ADD COLUMN IF NOT EXISTS asientos JSON"))
    connection.execute(text("ALTER TABLE reservas_stock ADD COLUMN IF NOT EXISTS total_centimos INTEGER NOT NULL DEFAULT 0"))


def install(app, models, get_db):
    Evento, Asiento, Reserva = models.Evento, models.Asiento, models.ReservaStock

    @app.get("/inventario/estado")
    def inventory(db=Depends(get_db), claims=Depends(actor)):
        if claims["role"] != "administrador":
            raise HTTPException(403, "Se requiere administrador")
        result = []
        for event in db.query(Evento).order_by(Evento.id).with_for_update(read=True):
            reserved = confirmed = 0
            for reservation in db.query(Reserva).filter_by(evento_id=event.id):
                if reservation.estado == "reservada":
                    reserved += reservation.cantidad
                elif reservation.estado == "confirmada":
                    confirmed += reservation.cantidad
            result.append({"evento_id": event.id, "inicial": event.stock_inicial, "disponible": event.stock,
                "reservadas": reserved, "vendidas": confirmed,
                "consistente": event.stock_inicial == event.stock + reserved + confirmed})
        return {"eventos": result, "consistente": all(e["consistente"] for e in result),
                "ecuacion": "inicial = disponible + reservadas + confirmadas (sin doble conteo)"}

    @app.post("/eventos", status_code=201)
    def create(data: EventSpec, db=Depends(get_db), claims=Depends(actor)):
        if claims["role"] not in {"administrador", "organizador"}:
            raise HTTPException(403, "Se requiere organizador o administrador")
        if not data.nombre.strip() or data.fecha.tzinfo is None:
            raise HTTPException(422, "Nombre y fecha con zona horaria requeridos")
        if data.asientos and (len(data.asientos) != data.capacidad or len({s.codigo for s in data.asientos}) != data.capacidad):
            raise HTTPException(422, "La capacidad debe coincidir con los asientos únicos")
        # El catálogo legacy sembró IDs explícitos; no se depende de su secuencia.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(7419251040)"))
        next_id = max(db.query(func.max(Evento.id)).scalar() or 0,
                      db.query(func.max(models.AuditoriaCatalogo.evento_id)).scalar() or 0) + 1
        event = Evento(id=next_id, nombre=data.nombre.strip(), descripcion=data.descripcion, lugar=data.lugar,
                       categoria=data.categoria, imagen_url=data.imagen_url,
                       fecha=data.fecha, precio_centimos=data.precio_centimos,
                       stock=data.capacidad, stock_inicial=data.capacidad,
                       organizador=f"{claims['iss']}|{claims['sub']}")
        db.add(event)
        db.flush()
        for seat in data.asientos:
            db.add(Asiento(evento_id=event.id, **seat.model_dump()))
        db.add(models.AuditoriaCatalogo(evento_id=event.id, actor=f"{claims['iss']}|{claims['sub']}", accion="crear"))
        db.commit()
        return serialize(event)

    @app.put("/eventos/{event_id}")
    def edit(event_id: int, data: EventEdit, db=Depends(get_db), claims=Depends(actor)):
        event = db.query(Evento).filter_by(id=event_id).with_for_update().first()
        if not event:
            raise HTTPException(404, "Evento no encontrado")
        owner(event, claims)
        if event.cancelado_en is not None and data.activo:
            raise HTTPException(409, "Un evento cancelado no puede reabrirse; cree un evento nuevo")
        if not data.nombre.strip() or data.fecha.tzinfo is None:
            raise HTTPException(422, "Nombre y fecha con zona horaria requeridos")
        if data.precio_centimos is not None and data.precio_centimos != event.precio_centimos and db.query(Asiento).filter_by(evento_id=event_id).first():
            raise HTTPException(409, "Los precios por asiento son independientes; no se puede cambiar el precio general de este evento")
        for key, value in data.model_dump(exclude_none=True).items():
            setattr(event, key, value)
        db.add(models.AuditoriaCatalogo(evento_id=event.id, actor=f"{claims['iss']}|{claims['sub']}", accion="editar"))
        db.commit()
        return serialize(event)

    @app.delete("/eventos/{event_id}")
    def delete(event_id: int, db=Depends(get_db), claims=Depends(actor)):
        event = db.query(Evento).filter_by(id=event_id).with_for_update().first()
        if not event:
            raise HTTPException(404, "Evento no encontrado")
        owner(event, claims)
        if event.stock != event.stock_inicial or event.cancelado_en is not None or db.query(Reserva).filter_by(evento_id=event_id).first() or db.query(models.OperacionCerrada).filter_by(evento_id=event_id).first():
            raise HTTPException(409, "El evento conserva reservas, ventas o compensaciones; use cancelación para preservar su historial")
        db.add(models.AuditoriaCatalogo(evento_id=event_id, actor=f"{claims['iss']}|{claims['sub']}", accion="eliminar"))
        db.query(Asiento).filter_by(evento_id=event_id).delete(synchronize_session=False)
        db.delete(event)
        db.commit()
        return {"estado": "eliminado", "evento_id": event_id}

    @app.get("/eventos/{event_id}/asientos")
    def seats(event_id: int, db=Depends(get_db)):
        return [{"codigo": s.codigo, "zona": s.zona, "precio_centimos": s.precio_centimos,
                 "disponible": s.operacion_id is None} for s in db.query(Asiento).filter_by(evento_id=event_id).order_by(Asiento.codigo)]

    @app.get("/eventos/{event_id}/autorizar-acceso")
    def access(event_id: int, db=Depends(get_db), claims=Depends(actor)):
        event = db.get(Evento, event_id)
        if not event:
            raise HTTPException(404, "Evento no encontrado")
        owner(event, claims)
        return {"autorizado": True, "evento_id": event_id}

    @app.post("/eventos/{event_id}/devolver")
    def refund(event_id: int, operacion_id: str, db=Depends(get_db), _service=Depends(require_service_token)):
        # Tombstone si reservar estaba en tránsito: serializa por evento y evita
        # que una petición tardía vuelva a descontar después de la compensación.
        if not operacion_id.startswith("ord-") or len(operacion_id) > 64:
            raise HTTPException(409, "Devolución reservada al coordinador de pedidos")
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:op, 0))"), {"op": operacion_id})
        closed = db.get(models.OperacionCerrada, operacion_id)
        if closed and closed.evento_id != event_id:
            raise HTTPException(409, "Operación de otro evento")
        if not closed:
            db.add(models.OperacionCerrada(operacion_id=operacion_id, evento_id=event_id))
        event = db.query(Evento).filter_by(id=event_id).with_for_update().first()
        if not event:
            db.commit()
            return {"estado": "liberada", "operacion_id": operacion_id, "evento_id": event_id}
        reservation = db.query(Reserva).filter_by(operacion_id=operacion_id).with_for_update().first()
        if not operacion_id.startswith("ord-"):
            raise HTTPException(409, "Devolución reservada al coordinador de pedidos")
        if reservation is None:
            reservation = Reserva(operacion_id=operacion_id, evento_id=event_id, cantidad=1, estado="liberada")
            db.add(reservation)
        elif reservation.evento_id != event_id:
            raise HTTPException(409, "Operación de otro evento")
        elif reservation.estado != "liberada":
            if event.stock_inicial is not None and event.stock + reservation.cantidad > event.stock_inicial:
                raise HTTPException(409, "Stock no verificable; revisar auditoría")
            event.stock += reservation.cantidad
            reservation.estado = "liberada"
            db.query(Asiento).filter_by(operacion_id=operacion_id).update({"operacion_id": None})
        db.commit()
        return {"estado": "liberada", "operacion_id": operacion_id, "evento_id": event_id}

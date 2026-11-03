"""Catálogo administrable, asientos e inventario reutilizado por el checkout."""
from datetime import datetime
from typing import List
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field
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
    fecha: datetime
    capacidad: int = Field(ge=1, le=100000)
    precio_centimos: int = Field(ge=0, le=1000000)
    asientos: List[SeatSpec] = Field(default_factory=list, max_length=10000)


class EventEdit(BaseModel):
    model_config = {"extra": "forbid"}
    nombre: str = Field(min_length=1, max_length=200)
    descripcion: str = Field(default="", max_length=4000)
    lugar: str = Field(default="", max_length=200)
    fecha: datetime
    activo: bool = True


def actor(authorization: str | None = Header(default=None)):
    return authenticated_claims(authorization)


def owner(event, claims):
    if claims["role"] not in {"administrador", "organizador"}:
        raise HTTPException(403, "Se requiere organizador o administrador")
    if claims["role"] != "administrador" and event.organizador != f"{claims['iss']}|{claims['sub']}":
        raise HTTPException(403, "No administras este evento")


def serialize(event):
    return {k: (getattr(event, k).isoformat() if k == "fecha" and event.fecha else getattr(event, k))
            for k in ("id", "nombre", "stock", "stock_inicial", "descripcion", "lugar", "fecha", "precio_centimos", "activo")}


def migrate(connection):
    # Aditivo bajo el advisory lock de Eventos. Nunca recalcula stock existente.
    for column in ("descripcion TEXT NOT NULL DEFAULT ''", "lugar VARCHAR(200) NOT NULL DEFAULT ''",
                   "fecha TIMESTAMPTZ", "precio_centimos INTEGER NOT NULL DEFAULT 0",
                   "organizador VARCHAR(128)", "activo BOOLEAN NOT NULL DEFAULT true"):
        connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS " + column))
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
        next_id = (db.query(func.max(Evento.id)).scalar() or 0) + 1
        event = Evento(id=next_id, nombre=data.nombre.strip(), descripcion=data.descripcion, lugar=data.lugar,
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
        if not data.nombre.strip() or data.fecha.tzinfo is None:
            raise HTTPException(422, "Nombre y fecha con zona horaria requeridos")
        for key, value in data.model_dump().items():
            setattr(event, key, value)
        db.add(models.AuditoriaCatalogo(evento_id=event.id, actor=f"{claims['iss']}|{claims['sub']}", accion="editar"))
        db.commit()
        return serialize(event)

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

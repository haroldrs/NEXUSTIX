import logging
"""Catálogo administrable, asientos e inventario reutilizado por el checkout."""
from datetime import datetime, timezone
from typing import List, Literal
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text, func, case
from shared.auth_tokens import authenticated_claims, require_service_token


class SeatSpec(BaseModel):
    codigo: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    zona: str = Field(min_length=1, max_length=80)
    precio_centimos: int = Field(ge=0, le=1000000)


class ZoneSpec(BaseModel):
    model_config = {"extra": "forbid"}
    nombre: str = Field(min_length=1, max_length=80)
    precio_centimos: int = Field(ge=0, le=1000000)
    color: str = Field(default="#537c68", pattern=r"^#[0-9a-fA-F]{6}$")
    capacidad: int | None = Field(default=None, ge=1, le=10000)
    filas: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("nombre")
    @classmethod
    def name(cls, value):
        if not value.strip():
            raise ValueError("Nombre de zona requerido")
        return value.strip()


class LayoutSpec(BaseModel):
    model_config = {"extra": "forbid"}
    tipo: Literal["general", "zonas", "asientos"]
    filas: list[str] = Field(default_factory=list, max_length=100)
    columnas: int = Field(default=1, ge=1, le=100)
    inicio: int = Field(default=1, ge=1, le=9999)
    pasillos: list[int] = Field(default_factory=list, max_length=99)
    zonas: list[ZoneSpec] = Field(default_factory=list, max_length=100)


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
    distribucion: LayoutSpec | None = None

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
        logging.getLogger("nexustix.audit").warning("evento_ajeno actor=%s|%s evento=%s", claims["iss"], claims["sub"], event.id)
        raise HTTPException(403, "No administras este evento")


def serialize(event, precio_asiento=None):
    result = {k: (getattr(event, k).isoformat() if k in {"fecha", "cancelado_en"} and getattr(event, k) else getattr(event, k))
            for k in ("id", "nombre", "stock", "stock_inicial", "descripcion", "lugar", "categoria", "imagen_url", "fecha", "precio_centimos", "activo", "cancelado_en", "motivo_cancelacion")}
    result["precio_desde_centimos"] = precio_asiento if precio_asiento is not None else event.precio_centimos
    result["distribucion"] = event.distribucion
    return result


def layout_inventory(data):
    """Genera una sola fuente de inventario; los pasillos son separadores."""
    layout = data.distribucion
    if layout is None:
        return {"tipo": "asientos" if data.asientos else "general"}, data.asientos
    if data.asientos:
        raise HTTPException(422, "Use distribución o asientos manuales, no ambos")
    if layout.tipo == "general":
        if layout.zonas or layout.filas or layout.pasillos:
            raise HTTPException(422, "La admisión general no admite zonas ni filas")
        return {"tipo": "general"}, []
    if not layout.zonas or len({z.nombre.casefold() for z in layout.zonas}) != len(layout.zonas):
        raise HTTPException(422, "Se requieren zonas con nombres únicos")
    seats = []
    if layout.tipo == "zonas":
        if layout.filas or layout.pasillos or any(z.filas or z.capacidad is None for z in layout.zonas):
            raise HTTPException(422, "Cada zona requiere capacidad y no admite filas ni pasillos")
        if sum(z.capacidad for z in layout.zonas) != data.capacidad or data.capacidad > 10000:
            raise HTTPException(422, "La suma de capacidades debe coincidir con el evento (máximo 10000)")
        for index, zone in enumerate(layout.zonas):
            seats.extend(SeatSpec(codigo=f"Z{index + 1}_{number + 1}", zona=zone.nombre,
                                  precio_centimos=zone.precio_centimos) for number in range(zone.capacidad))
    else:
        import re
        if (not layout.filas or len(set(layout.filas)) != len(layout.filas)
                or any(not re.fullmatch(r"[A-Za-z][A-Za-z_-]{0,15}", row) for row in layout.filas)):
            raise HTTPException(422, "Use filas únicas de hasta 16 letras, guiones o guiones bajos")
        if len(layout.filas) * layout.columnas != data.capacidad or data.capacidad > 10000:
            raise HTTPException(422, "Filas × columnas debe coincidir con la capacidad (máximo 10000)")
        assigned = [row for zone in layout.zonas for row in zone.filas]
        if (len(assigned) != len(set(assigned)) or set(assigned) != set(layout.filas)
                or any(zone.capacidad is not None or not zone.filas for zone in layout.zonas)):
            raise HTTPException(422, "Asigne cada fila a una sola zona con precio")
        if len(set(layout.pasillos)) != len(layout.pasillos) or any(p < 1 or p >= layout.columnas for p in layout.pasillos):
            raise HTTPException(422, "Los pasillos separan columnas existentes y no son asientos")
        for row in layout.filas:
            zone = next(z for z in layout.zonas if row in z.filas)
            seats.extend(SeatSpec(codigo=f"{row}{number}", zona=zone.nombre, precio_centimos=zone.precio_centimos)
                         for number in range(layout.inicio, layout.inicio + layout.columnas))
    return layout.model_dump(exclude_none=True), seats


def sale_open(event):
    return event.activo and event.cancelado_en is None and (event.fecha is None or
        event.fecha.replace(tzinfo=event.fecha.tzinfo or timezone.utc) > datetime.now(timezone.utc))


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
    connection.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS distribucion JSON"))
    connection.execute(text("ALTER TABLE reservas_stock ADD COLUMN IF NOT EXISTS zona VARCHAR(80)"))


def install(app, models, get_db):
    Evento, Asiento, Reserva = models.Evento, models.Asiento, models.ReservaStock

    @app.get("/auditoria/catalogo")
    def catalog_audit(db=Depends(get_db), claims=Depends(actor)):
        if claims["role"] != "administrador":
            raise HTTPException(403, "Se requiere administrador")
        rows = db.query(models.AuditoriaCatalogo).order_by(models.AuditoriaCatalogo.id.desc()).limit(100).all()
        return {"registros": [{"accion": row.accion, "recurso": f"evento:{row.evento_id}", "actor": row.actor,
            "momento": row.creado_en.isoformat() if row.creado_en else None} for row in rows]}

    @app.get("/inventario/estado")
    def inventory(db=Depends(get_db), claims=Depends(actor)):
        if claims["role"] != "administrador":
            raise HTTPException(403, "Se requiere administrador")
        result = []
        events = db.query(Evento).order_by(Evento.id).with_for_update(read=True).all()
        totals = {(event_id, state): amount for event_id, state, amount in
            db.query(Reserva.evento_id, Reserva.estado, func.sum(Reserva.cantidad)).group_by(Reserva.evento_id, Reserva.estado)}
        # Cada plaza con zona es una fila propia: su reserva se identifica por operacion_id,
        # sin repartir cantidades entre zonas.
        zones = {}
        for event_id, zone, occupied, state, amount in db.query(Asiento.evento_id, Asiento.zona, Asiento.operacion_id.isnot(None), Reserva.estado, func.count(Asiento.id)).outerjoin(
                Reserva, Asiento.operacion_id == Reserva.operacion_id).group_by(Asiento.evento_id, Asiento.zona, Asiento.operacion_id.isnot(None), Reserva.estado):
            row = zones.setdefault((event_id, zone), {"zona": zone, "capacidad": 0, "disponibles": 0, "reservadas": 0, "confirmadas": 0, "liberadas_con_plaza": 0, "sin_reserva": 0})
            row["capacidad"] += amount
            key = "disponibles" if not occupied else {"reservada": "reservadas", "confirmada": "confirmadas", "liberada": "liberadas_con_plaza"}.get(state, "sin_reserva")
            row[key] += amount
        for row in zones.values():
            row["verificacion"] = "inconsistente" if row["liberadas_con_plaza"] else "no_verificable" if row["sin_reserva"] else "consistente"
        for event in events:
            reserved, confirmed = int(totals.get((event.id, "reservada"), 0)), int(totals.get((event.id, "confirmada"), 0))
            consistent = event.stock_inicial == event.stock + reserved + confirmed
            result.append({"evento_id": event.id, "nombre": event.nombre, "inicial": event.stock_inicial, "disponible": event.stock,
                "reservadas": reserved, "vendidas": confirmed, "consistente": consistent,
                "verificacion": "no_verificable" if event.stock_inicial is None else "consistente" if consistent else "inconsistente",
                "zonas": sorted((row for key, row in zones.items() if key[0] == event.id), key=lambda row: row["zona"])})
        return {"eventos": result, "consistente": all(e["consistente"] for e in result),
                "observado_en": datetime.now(timezone.utc).isoformat(),
                "ecuacion": "inicial = disponible + reservadas + confirmadas (sin doble conteo)"}

    @app.post("/eventos", status_code=201)
    def create(data: EventSpec, db=Depends(get_db), claims=Depends(actor)):
        if claims["role"] not in {"administrador", "organizador"}:
            raise HTTPException(403, "Se requiere organizador o administrador")
        if not data.nombre.strip() or data.fecha.tzinfo is None:
            raise HTTPException(422, "Nombre y fecha con zona horaria requeridos")
        layout, seats = layout_inventory(data)
        if seats and (len(seats) != data.capacidad or len({s.codigo for s in seats}) != data.capacidad):
            raise HTTPException(422, "La capacidad debe coincidir con los asientos únicos")
        if any(not seat.zona.strip() for seat in seats):
            raise HTTPException(422, "Cada asiento requiere una zona válida")
        # El catálogo legacy sembró IDs explícitos; no se depende de su secuencia.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(7419251040)"))
        next_id = max(db.query(func.max(Evento.id)).scalar() or 0,
                      db.query(func.max(models.AuditoriaCatalogo.evento_id)).scalar() or 0) + 1
        event = Evento(id=next_id, nombre=data.nombre.strip(), descripcion=data.descripcion, lugar=data.lugar,
                       categoria=data.categoria, imagen_url=data.imagen_url,
                       fecha=data.fecha, precio_centimos=data.precio_centimos, distribucion=layout,
                       stock=data.capacidad, stock_inicial=data.capacidad,
                       organizador=f"{claims['iss']}|{claims['sub']}")
        db.add(event)
        db.flush()
        for seat in seats:
            db.add(Asiento(evento_id=event.id, **seat.model_dump()))
        db.add(models.AuditoriaCatalogo(evento_id=event.id, actor=f"{claims['iss']}|{claims['sub']}", accion="crear"))
        db.commit()
        return serialize(event, min((seat.precio_centimos for seat in seats), default=None))

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
        event = db.get(Evento, event_id)
        if not event:
            raise HTTPException(404, "Evento no encontrado")
        if (event.distribucion or {}).get("tipo") == "zonas":
            return []
        available = sale_open(event)
        return [{"codigo": s.codigo, "zona": s.zona, "precio_centimos": s.precio_centimos,
                 "disponible": available and s.operacion_id is None,
                 "estado": "no_disponible" if not available else "ocupado" if state == "confirmada" else
                           "reservado" if s.operacion_id else "disponible"}
                for s, state in db.query(Asiento, Reserva.estado).outerjoin(Reserva, Asiento.operacion_id == Reserva.operacion_id)
                .filter(Asiento.evento_id == event_id).order_by(Asiento.codigo)]

    @app.get("/eventos/{event_id}/zonas")
    def zones(event_id: int, db=Depends(get_db)):
        event = db.get(Evento, event_id)
        if not event:
            raise HTTPException(404, "Evento no encontrado")
        layout = event.distribucion or {}
        if layout.get("tipo") != "zonas":
            return []
        colors = {z["nombre"]: z["color"] for z in layout["zonas"]}
        available = sale_open(event)
        return [{"nombre": name, "capacidad": capacity, "disponibles": free if available else 0,
                 "precio_centimos": price, "color": colors.get(name, "#537c68")}
                for name, capacity, free, price in db.query(Asiento.zona, func.count(Asiento.id),
                    func.sum(case((Asiento.operacion_id.is_(None), 1), else_=0)), func.min(Asiento.precio_centimos))
                .filter(Asiento.evento_id == event_id).group_by(Asiento.zona).order_by(Asiento.zona)]

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

from typing import Annotated
import os
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from sqlalchemy import text, func, case
from database import Base, SessionLocal, engine, get_db
from models import Evento, ReservaStock, Asiento, OperacionCerrada
import models
from shared import catalog, telemetry
from shared.auth_tokens import authenticated_claims, require_service_token


@asynccontextmanager
async def _lifespan(application: FastAPI):
    esperar_bd()
    db = SessionLocal()
    try:
        if os.getenv("NEXUSTIX_SEED_EVENTS", "1") == "1" and db.query(Evento).count() == 0:
            seed = [Evento(**d, stock_inicial=d["stock"]) for d in EVENTOS_BASE]
            db.add_all(seed)
            db.commit()
    finally:
        db.close()
    yield


app = FastAPI(title="Servicio de Eventos", lifespan=_lifespan)
catalog.install(app, models, get_db)
telemetry.install(app, "eventos")

# Permite que la interfaz web (servida en otro puerto/origen) consuma esta API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080").split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
)


def require_admin(authorization: str | None = Header(default=None)):
    claims = authenticated_claims(authorization)
    if claims.get("role") != "administrador":
        raise HTTPException(status_code=403, detail="Se requiere rol administrador")
    return claims


EVENTOS_BASE = [
    {"id": 1, "nombre": "Concierto Rock en Trujillo", "stock": 500, "descripcion": "Una noche de rock en vivo con artistas locales e invitados.", "lugar": "Club Metropolitano, Trujillo", "fecha": datetime(2027, 3, 14, 20, tzinfo=timezone.utc), "precio_centimos": 7500},
    {"id": 2, "nombre": "Festival de Jazz", "stock": 20, "descripcion": "Un encuentro íntimo para disfrutar jazz contemporáneo y clásico.", "lugar": "Teatro Municipal, Lima", "fecha": datetime(2027, 4, 9, 19, 30, tzinfo=timezone.utc), "precio_centimos": 9500},
    {"id": 3, "nombre": "Concierto Pop Lima", "stock": 300, "descripcion": "Los éxitos pop que marcaron una generación, en una sola noche.", "lugar": "Anfiteatro del Parque, Lima", "fecha": datetime(2027, 5, 22, 20, tzinfo=timezone.utc), "precio_centimos": 8500},
    {"id": 4, "nombre": "Festival Electrónico", "stock": 250, "descripcion": "Sets de DJs, visuales inmersivas y una experiencia electrónica al aire libre.", "lugar": "Costa Verde, Lima", "fecha": datetime(2027, 6, 12, 18, tzinfo=timezone.utc), "precio_centimos": 11000},
    {"id": 5, "nombre": "Concierto Internacional", "stock": 400, "descripcion": "Un artista internacional y producción de gran formato para una noche única.", "lugar": "Arena Lima", "fecha": datetime(2027, 7, 3, 20, 30, tzinfo=timezone.utc), "precio_centimos": 14500},
]


def esperar_bd():
    intentos = 10
    while intentos > 0:
        try:
            # Cambios idempotentes y aditivos para bases que ya existían antes
            # de cada mejora (Eventos no usa Alembic). Un advisory lock evita
            # carreras si se arrancan dos instancias a la vez.
            with engine.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(7419251033)"))
                Base.metadata.create_all(bind=conn)
                catalog.migrate(conn)
                conn.execute(text("""DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_eventos_stock_no_negativo') THEN
                        ALTER TABLE eventos ADD CONSTRAINT ck_eventos_stock_no_negativo CHECK (stock >= 0) NOT VALID;
                    END IF;
                END $$;"""))
                conn.execute(text("ALTER TABLE eventos ADD COLUMN IF NOT EXISTS stock_inicial INTEGER"))
                conn.execute(text("""DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_eventos_stock_inicial_no_negativo') THEN
                        ALTER TABLE eventos ADD CONSTRAINT ck_eventos_stock_inicial_no_negativo
                            CHECK (stock_inicial IS NULL OR stock_inicial >= 0) NOT VALID;
                    END IF;
                END $$;"""))
                # Solo se completan los eventos base, cuyo aforo original es
                # conocido; nunca se sobrescribe un valor ya registrado.
                for base in EVENTOS_BASE:
                    conn.execute(
                        text("UPDATE eventos SET stock_inicial = :stock WHERE id = :id AND stock_inicial IS NULL"),
                        {"stock": base["stock"], "id": base["id"]},
                    )
            return
        except OperationalError:
            intentos -= 1
            time.sleep(3)
    raise RuntimeError("No se pudo conectar a la base de datos de eventos")


def stock_inicial_de(evento: Evento) -> int | None:
    """Aforo original: columna persistida o, para filas antiguas, el catálogo base."""
    if evento.stock_inicial is not None:
        return evento.stock_inicial
    return next((base["stock"] for base in EVENTOS_BASE if base["id"] == evento.id), None)


# ---------- Reloj Lógico de Lamport ----------
RELOJ_LAMPORT = 0
_RELOJ_LOCK = Lock()


def tic_lamport(t_recibido: int = 0) -> int:
    """Avanza el reloj logico de Lamport: max(L_local, L_msg) + 1."""
    global RELOJ_LAMPORT
    with _RELOJ_LOCK:
        RELOJ_LAMPORT = max(RELOJ_LAMPORT, t_recibido) + 1
        return RELOJ_LAMPORT


@app.get("/health")
def health(response: Response, db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.rollback()
        response.status_code = 503
        return {"status": "unavailable", "servicio": "eventos", "db": "unavailable"}
    return {
        "status": "ok",
        "servicio": "eventos",
        "db": "connected",
        "reloj_logico_T": tic_lamport(),
        "timestamp_fisico": time.time(),
    }


@app.get("/eventos")
def listar_eventos(db: Session = Depends(get_db)):
    eventos = db.query(Evento).all()
    prices = dict(db.query(Asiento.evento_id, func.coalesce(
        func.min(case((Asiento.operacion_id.is_(None), Asiento.precio_centimos))),
        func.min(Asiento.precio_centimos))).group_by(Asiento.evento_id).all())
    return [catalog.serialize(e, prices.get(e.id)) for e in eventos]


@app.post("/eventos/{evento_id}/cancelar")
def cancelar_evento(
    evento_id: int = Path(gt=0), motivo: str = Query(default="Cancelación administrativa", max_length=300),
    db: Session = Depends(get_db), admin: dict = Depends(require_admin),
):
    return cerrar_evento(db, evento_id, motivo, f"{admin['iss']}|{admin['sub']}")


def cerrar_evento(db, evento_id, motivo, actor):
    """La fila cancelada funciona como señal durable recuperable por Compras."""
    evento = db.query(Evento).filter(Evento.id == evento_id).with_for_update().first()
    if not evento:
        raise HTTPException(404, "Evento no encontrado")
    if evento.cancelado_en is not None:
        return {"evento_id": evento_id, "estado": "cancelado", "idempotente": True}
    evento.activo = False
    evento.motivo_cancelacion = motivo.strip() or "Cancelación administrativa"
    evento.cancelado_en = datetime.now(timezone.utc)
    evento.cancelado_por = actor
    db.add(models.AuditoriaCatalogo(evento_id=evento_id, actor=actor, accion="cancelar"))
    db.commit()
    return {"evento_id": evento_id, "estado": "cancelado", "idempotente": False}


@app.post("/interno/eventos/{evento_id}/cancelar")
def cerrar_evento_interno(evento_id: int = Path(gt=0), motivo: str = Query(max_length=300),
                         actor: str = Query(min_length=1, max_length=128), db: Session = Depends(get_db),
                         _service: None = Depends(require_service_token)):
    # Solo el coordinador puede reanudar una intención administrativa persistida.
    return cerrar_evento(db, evento_id, motivo, actor)


@app.get("/interno/eventos/cancelados")
def cancelaciones_pendientes(after_id: int = Query(default=0, ge=0), db: Session = Depends(get_db),
                            _service: None = Depends(require_service_token)):
    rows = db.query(Evento).filter(Evento.cancelado_en.isnot(None), Evento.id > after_id).order_by(Evento.id).limit(100).all()
    return {"items": [{"evento_id": e.id, "motivo": e.motivo_cancelacion,
                       "solicitada_por": e.cancelado_por or "catalogo:administrador",
                       "cancelado_en": e.cancelado_en.isoformat()} for e in rows]}


@app.get("/interno/eventos/{evento_id}/reservas-activas")
def reservas_del_evento(evento_id: int = Path(gt=0), db: Session = Depends(get_db),
                       _service: None = Depends(require_service_token)):
    event = db.get(Evento, evento_id)
    if not event:
        raise HTTPException(404, "Evento no encontrado")
    rows = db.query(ReservaStock).filter(ReservaStock.evento_id == evento_id, ReservaStock.estado != "liberada").all()
    return {"evento_id": evento_id, "cancelado": event.cancelado_en is not None,
            "reservas": [{"operacion_id": r.operacion_id, "cantidad": r.cantidad,
                           "estado": r.estado, "total_centimos": r.total_centimos} for r in rows]}

@app.get("/eventos/auditoria/stock")
def auditoria_eventos(db: Session = Depends(get_db), _service: None = Depends(require_service_token)):
    """Stock actual/original y operaciones confirmadas registradas por Eventos."""
    eventos = db.query(Evento).order_by(Evento.id).all()
    confirmadas = db.query(ReservaStock).filter(ReservaStock.estado == "confirmada").all()
    operaciones_por_evento = {}
    for reserva in confirmadas:
        operaciones_por_evento.setdefault(reserva.evento_id, []).append({
            "operacion_id": reserva.operacion_id,
            "cantidad": reserva.cantidad,
        })
    return [
        {
            "id": e.id,
            "nombre": e.nombre,
            "stock_actual": e.stock,
            "stock_inicial": stock_inicial_de(e),
            "operaciones_confirmadas": sorted(
                operaciones_por_evento.get(e.id, []),
                key=lambda op: (
                    str(op.get("operacion_id", "")),
                    int(op.get("cantidad", 0)),
                ) if isinstance(op, dict) else str(op),
            ),
        }
        for e in eventos
    ]



@app.get("/eventos/reservas/pendientes")
def reservas_pendientes(db: Session = Depends(get_db), _service: None = Depends(require_service_token)):
    """Lista reservas aún no confirmadas ni liberadas para reconciliación tras un fallo."""
    filas = db.query(ReservaStock).filter(ReservaStock.estado == "reservada").order_by(ReservaStock.creado_en).all()
    return {
        "total": len(filas),
        "reservas": [
            {"operacion_id": r.operacion_id, "evento_id": r.evento_id, "cantidad": r.cantidad, "estado": r.estado}
            for r in filas
        ],
    }


@app.get("/eventos/reservas/{operacion_id}")
def consultar_reserva_por_operacion(
    operacion_id: str = Path(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    _service: None = Depends(require_service_token),
):
    """Devuelve el estado durable de una reserva para recuperación de Compras."""
    reserva = db.query(ReservaStock).filter(ReservaStock.operacion_id == operacion_id).first()
    if reserva is None:
        raise HTTPException(status_code=404, detail="No existe una reserva para esta operación")
    evento = db.query(Evento).filter(Evento.id == reserva.evento_id).first()
    if evento is None:
        raise HTTPException(status_code=503, detail="La reserva existe pero el evento asociado no está disponible")
    return {
        "operacion_id": reserva.operacion_id,
        "evento_id": reserva.evento_id,
        "cantidad": reserva.cantidad,
        "estado": reserva.estado,
        "stock_actual": evento.stock,
        "asientos": [] if reserva.zona else reserva.asientos or [], "total_centimos": reserva.total_centimos,
        "zona": reserva.zona,
    }


@app.get("/eventos/{evento_id}")
def obtener_evento(evento_id: int, db: Session = Depends(get_db)):
    evento = db.query(Evento).filter(Evento.id == evento_id).first()
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    price = db.query(func.coalesce(
        func.min(case((Asiento.operacion_id.is_(None), Asiento.precio_centimos))),
        func.min(Asiento.precio_centimos))).filter(Asiento.evento_id == evento_id).scalar()
    return catalog.serialize(evento, price)


@app.get("/eventos/{evento_id}/stock")
def consultar_stock(evento_id: int, db: Session = Depends(get_db)):
    evento = db.query(Evento).filter(Evento.id == evento_id).first()
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    return {"evento_id": evento.id, "stock": evento.stock, "reloj_logico_T": tic_lamport()}



@app.get("/eventos/reconciliar-stock/preview")
def previsualizar_reconciliacion_stock(db: Session = Depends(get_db), _admin: dict = Depends(require_admin)):
    """Calcula el stock esperado usando las reservas confirmadas, sin modificar datos."""
    base = {e["id"]: e["stock"] for e in EVENTOS_BASE}
    eventos = {e.id: e for e in db.query(Evento).with_for_update(read=True).all()}
    confirmadas = db.query(ReservaStock).filter(ReservaStock.estado == "confirmada").all()
    vendidas = {}
    for r in confirmadas:
        vendidas[r.evento_id] = vendidas.get(r.evento_id, 0) + r.cantidad
    resultado = []
    for evento_id, stock_inicial in base.items():
        actual = eventos[evento_id].stock if evento_id in eventos else None
        cantidad_confirmada = vendidas.get(evento_id, 0)
        esperado = stock_inicial - cantidad_confirmada
        resultado.append({
            "evento_id": evento_id,
            "nombre": eventos[evento_id].nombre if evento_id in eventos else next(e["nombre"] for e in EVENTOS_BASE if e["id"] == evento_id),
            "stock_actual": actual,
            "entradas_confirmadas": cantidad_confirmada,
            "stock_esperado": esperado,
            "requiere_cambio": actual != esperado,
        })
    return {
        "modo": "previsualizacion_sin_cambios",
        "reservas_pendientes": db.query(ReservaStock).filter(ReservaStock.estado == "reservada").count(),
        "eventos": resultado,
        "nota": "El cálculo usa las reservas confirmadas de la base de datos de eventos. Verifique que coincidan con las compras persistidas antes de aplicar.",
    }


@app.post("/eventos/reconciliar-stock")
def reconciliar_stock(aplicar: bool = Query(default=False), db: Session = Depends(get_db), _admin: dict = Depends(require_admin)):
    """Recalcula stock desde reservas confirmadas. Por defecto solo previsualiza; aplicar=true escribe."""
    pendientes = db.query(ReservaStock).filter(ReservaStock.estado == "reservada").count()
    if pendientes:
        raise HTTPException(status_code=409, detail=f"Hay {pendientes} reservas pendientes; ejecute recuperación antes de reconciliar.")

    base = {e["id"]: e for e in EVENTOS_BASE}
    eventos = {e.id: e for e in db.query(Evento).with_for_update().all()}
    confirmadas = db.query(ReservaStock).filter(ReservaStock.estado == "confirmada").all()
    vendidas = {}
    for reserva in confirmadas:
        vendidas[reserva.evento_id] = vendidas.get(reserva.evento_id, 0) + reserva.cantidad

    resultado = []
    for evento_id, datos in base.items():
        vendido = vendidas.get(evento_id, 0)
        esperado = datos["stock"] - vendido
        if esperado < 0:
            raise HTTPException(status_code=409, detail=f"Las reservas confirmadas exceden el stock inicial del evento {evento_id}; no se modificó ningún stock.")
        evento = eventos.get(evento_id)
        actual = evento.stock if evento else None
        resultado.append({"evento_id": evento_id, "nombre": datos["nombre"], "stock_anterior": actual, "entradas_confirmadas": vendido, "stock_esperado": esperado, "cambiaria": actual != esperado})

    if not aplicar:
        return {"status": "preview", "aplicado": False, "eventos": resultado, "mensaje": "No se modificó ningún dato. Revise el resultado y repita con ?aplicar=true para aplicar."}

    for fila in resultado:
        evento = eventos.get(fila["evento_id"])
        if evento:
            evento.stock = fila["stock_esperado"]
            evento.nombre = fila["nombre"]
            if evento.stock_inicial is None:
                evento.stock_inicial = base[fila["evento_id"]]["stock"]
        else:
            db.add(Evento(id=fila["evento_id"], nombre=fila["nombre"], stock=fila["stock_esperado"],
                          stock_inicial=base[fila["evento_id"]]["stock"]))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Falló la reconciliación; se revirtió la transacción.")
    return {"status": "reconciliacion_completa", "aplicado": True, "eventos": resultado, "reloj_logico_T": tic_lamport(), "mensaje": "Stock reconciliado usando reservas confirmadas; no se modificaron compras ni reservas."}


@app.post("/eventos/reset")
def resetear_eventos(db: Session = Depends(get_db), _admin: dict = Depends(require_admin)):
    """Restablece el stock solo cuando no existen compras/reservas confirmadas ni pendientes."""
    reservas_activas = db.query(ReservaStock).filter(ReservaStock.estado.in_(["reservada", "confirmada"])).count()
    if reservas_activas:
        raise HTTPException(
            status_code=409,
            detail=f"No se puede restablecer el stock mientras existan {reservas_activas} reservas pendientes o confirmadas. Use reconciliación para reparar el inventario, o el procedimiento completo de reset de pruebas que coordina todas las bases de datos.",
        )
    for datos in EVENTOS_BASE:
        evento = db.query(Evento).filter(Evento.id == datos["id"]).first()
        if evento:
            evento.stock = datos["stock"]
            evento.stock_inicial = datos["stock"]
            evento.nombre = datos["nombre"]
        else:
            db.add(Evento(id=datos["id"], nombre=datos["nombre"], stock=datos["stock"], stock_inicial=datos["stock"]))
    db.commit()
    return {"status": "ok", "mensaje": "Stock restablecido: no existían reservas activas ni confirmadas.", "reloj_logico_T": tic_lamport()}


@app.post("/eventos/{evento_id}/reservar")
def reservar_entradas(
    evento_id: int = Path(gt=0),
    cantidad: int = Query(default=1, ge=1, le=1000),
    reloj_t: int = Query(default=0, ge=0),
    operacion_id: str = Query(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    _service: None = Depends(require_service_token),
    asientos: str = "",
    zona: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
):
    """Reserva stock con bloqueo de fila y clave idempotente por operación."""
    t_inicio_lock = time.perf_counter()
    t_actual = tic_lamport(reloj_t)

    codigos = sorted(asientos.split(",")) if asientos else []
    if zona is not None:
        zona = zona.strip()
        if not zona or codigos:
            raise HTTPException(422, "Seleccione una zona y cantidad, sin códigos de asiento")
    if codigos and (len(codigos) != cantidad or len(set(codigos)) != cantidad):
        raise HTTPException(422, "Seleccione un asiento único por entrada")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:op, 0))"), {"op": operacion_id})
    if db.get(OperacionCerrada, operacion_id):
        raise HTTPException(409, "Operación cerrada; no puede reservarse de nuevo")
    evento = db.query(Evento).filter(Evento.id == evento_id).with_for_update().first()
    if not evento:
        raise HTTPException(404, "Evento no encontrado")
    if evento.cancelado_en is not None:
        raise HTTPException(409, "Evento cancelado")
    por_zona = (evento.distribucion or {}).get("tipo") == "zonas"
    if por_zona != (zona is not None):
        raise HTTPException(422, "Seleccione una zona para este evento" if por_zona else "Este evento no admite reserva por zona")
    existente = db.query(ReservaStock).filter(ReservaStock.operacion_id == operacion_id).with_for_update().first()
    if existente:
        if (existente.evento_id != evento_id or existente.cantidad != cantidad or existente.zona != zona
                or (not zona and sorted(existente.asientos or []) != codigos)):
            raise HTTPException(status_code=409, detail="operacion_id ya utilizado con datos diferentes")
        if existente.estado == "liberada":
            raise HTTPException(status_code=409, detail="Esta operación ya fue compensada y no puede reutilizarse")
        evento_existente = db.query(Evento).filter(Evento.id == evento_id).first()
        return {
            "evento_id": evento_id,
            "cantidad": existente.cantidad,
            "stock_restante": evento_existente.stock if evento_existente else None,
            "operacion_id": operacion_id,
            "estado_reserva": existente.estado,
            "total_centimos": existente.total_centimos, "asientos": [] if zona else existente.asientos or [],
            "zona": existente.zona,
            "idempotente": True,
            "reloj_logico_T": t_actual,
        }

    evento = db.query(Evento).filter(Evento.id == evento_id).with_for_update().first()
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    if not evento.activo or (evento.fecha and evento.fecha.replace(tzinfo=evento.fecha.tzinfo or timezone.utc) <= datetime.now(timezone.utc)):
        raise HTTPException(409, "Evento cerrado")
    seats = db.query(Asiento).filter_by(evento_id=evento_id).all()
    if zona:
        selected = sorted((seat for seat in seats if seat.zona == zona and seat.operacion_id is None), key=lambda seat: seat.id)[:cantidad]
        if len(selected) != cantidad:
            raise HTTPException(409, "La zona no dispone de la cantidad solicitada")
        codigos = [seat.codigo for seat in selected]
    else:
        selected = [seat for seat in seats if seat.codigo in codigos]
    if seats and (len(selected) != cantidad or any(seat.operacion_id for seat in selected)):
        raise HTTPException(409, "Asientos no disponibles; seleccione uno por entrada")
    if codigos and not seats:
        raise HTTPException(422, "Este evento es de admisión general")
    total = sum(seat.precio_centimos for seat in selected) if seats else evento.precio_centimos * cantidad
    if evento.stock < cantidad:
        raise HTTPException(status_code=409, detail=f"Stock insuficiente (disponible: {evento.stock}, solicitado: {cantidad})")

    for seat in selected:
        seat.operacion_id = operacion_id
    evento.stock -= cantidad
    db.add(ReservaStock(operacion_id=operacion_id, evento_id=evento_id, cantidad=cantidad, estado="reservada", asientos=codigos, zona=zona, total_centimos=total))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Dos reintentos simultáneos con la misma clave no deben descontar dos veces.
        existente = db.query(ReservaStock).filter(ReservaStock.operacion_id == operacion_id).first()
        if (existente and existente.evento_id == evento_id and existente.cantidad == cantidad and existente.zona == zona
                and (zona or sorted(existente.asientos or []) == sorted(codigos)) and existente.estado != "liberada"):
            evento_actual = db.query(Evento).filter(Evento.id == evento_id).first()
            return {"evento_id": evento_id, "cantidad": existente.cantidad, "stock_restante": evento_actual.stock,
                    "operacion_id": operacion_id, "estado_reserva": existente.estado, "idempotente": True,
                    "reloj_logico_T": t_actual, "total_centimos": existente.total_centimos, "zona": existente.zona,
                    "asientos": [] if existente.zona else existente.asientos or []}
        raise HTTPException(status_code=409, detail="Conflicto al registrar la reserva; vuelva a consultar su estado")
    db.refresh(evento)
    tiempo_exclusion_ms = round((time.perf_counter() - t_inicio_lock) * 1000, 3)
    return {
        "evento_id": evento.id,
        "cantidad": cantidad,
        "stock_restante": evento.stock,
        "operacion_id": operacion_id,
        "estado_reserva": "reservada",
        "total_centimos": total, "asientos": [] if zona else codigos, "zona": zona,
        "idempotente": False,
        "reloj_logico_T": t_actual,
        "exclusion_mutua": {
            "mecanismo": "SELECT ... FOR UPDATE + registro idempotente en PostgreSQL",
            "recurso_bloqueado": f"fila_evento_id_{evento.id}",
            "tiempo_seccion_critica_ms": tiempo_exclusion_ms,
            "estado": "Reserva atómica; cantidad positiva y stock no negativo",
        },
    }


@app.post("/eventos/{evento_id}/confirmar")
def confirmar_reserva(
    evento_id: int = Path(gt=0),
    operacion_id: str = Query(min_length=1, max_length=64),
    reloj_t: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _service: None = Depends(require_service_token),
):
    """Marca como confirmada una reserva cuya compra ya quedó persistida."""
    # Siempre evento -> reserva, igual que reservar y devolver, evita interbloqueos.
    evento = db.query(Evento).filter_by(id=evento_id).with_for_update().first()
    if not evento:
        raise HTTPException(404, "Evento no encontrado")
    if evento.cancelado_en is not None:
        raise HTTPException(409, "Evento cancelado; corresponde compensar la operación")
    reserva = db.query(ReservaStock).filter(ReservaStock.operacion_id == operacion_id).with_for_update().first()
    if not reserva or reserva.evento_id != evento_id:
        raise HTTPException(status_code=404, detail="Reserva no encontrada para ese evento")
    if reserva.estado == "liberada":
        raise HTTPException(status_code=409, detail="No se puede confirmar una reserva ya liberada")
    ya_confirmada = reserva.estado == "confirmada"
    if not ya_confirmada:
        reserva.estado = "confirmada"
        db.commit()
    return {"status": "ok", "evento_id": evento_id, "cantidad": reserva.cantidad, "operacion_id": operacion_id, "estado_reserva": reserva.estado, "idempotente": ya_confirmada, "reloj_logico_T": tic_lamport(reloj_t)}


@app.post("/eventos/{evento_id}/liberar")
def liberar_entradas(
    evento_id: int = Path(gt=0),
    cantidad: int = Query(default=1, ge=1, le=1000),
    reloj_t: int = Query(default=0, ge=0),
    operacion_id: str = Query(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    _service: None = Depends(require_service_token),
):
    """Compensa una reserva una sola vez; no permite inflar el stock por reintentos."""
    t_inicio_lock = time.perf_counter()
    t_actual = tic_lamport(reloj_t)
    evento = db.query(Evento).filter_by(id=evento_id).with_for_update().first()
    if not evento:
        raise HTTPException(404, "Evento no encontrado")
    reserva = db.query(ReservaStock).filter(ReservaStock.operacion_id == operacion_id).with_for_update().first()
    if not reserva or reserva.evento_id != evento_id:
        raise HTTPException(status_code=404, detail="Reserva no encontrada para compensar")
    if reserva.cantidad != cantidad:
        raise HTTPException(status_code=409, detail="La cantidad no coincide con la reserva original")
    if reserva.estado == "liberada":
        evento = db.query(Evento).filter(Evento.id == evento_id).first()
        return {"evento_id": evento_id, "cantidad": reserva.cantidad, "stock_restante": evento.stock if evento else None, "operacion_id": operacion_id, "estado_reserva": "liberada", "idempotente": True, "reloj_logico_T": t_actual}
    if reserva.estado == "confirmada":
        raise HTTPException(status_code=409, detail="La reserva ya fue confirmada; no puede liberarse como compensación de una compra abortada")

    evento = db.query(Evento).filter(Evento.id == evento_id).with_for_update().first()
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    stock_inicial = stock_inicial_de(evento)
    if stock_inicial is not None and evento.stock + cantidad > stock_inicial:
        raise HTTPException(status_code=409, detail="La compensación excedería el stock inicial; se requiere auditoría manual")
    evento.stock += cantidad
    reserva.estado = "liberada"
    db.query(Asiento).filter_by(operacion_id=operacion_id).update({"operacion_id": None})
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="No se pudo persistir la compensación")
    db.refresh(evento)
    tiempo_exclusion_ms = round((time.perf_counter() - t_inicio_lock) * 1000, 3)
    return {
        "evento_id": evento.id,
        "cantidad": reserva.cantidad,
        "stock_restante": evento.stock,
        "operacion_id": operacion_id,
        "estado_reserva": "liberada",
        "idempotente": False,
        "mensaje": f"Se liberaron {cantidad} entradas una sola vez para la operación {operacion_id}.",
        "reloj_logico_T": t_actual,
        "exclusion_mutua": {"mecanismo": "SELECT ... FOR UPDATE + compensación idempotente", "tiempo_seccion_critica_ms": tiempo_exclusion_ms},
    }


from shared import diagnostics
diagnostics.install(app, get_db, require_admin)

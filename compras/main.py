import os
import hashlib
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from collections import deque
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import List
from uuid import uuid4

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from database import SessionLocal, engine, get_db
from models import Compra, IdempotencyRequest, RegistroJournal
from shared.auth_tokens import authenticated_claims, service_auth_headers

app = FastAPI(title="Servicio de Compras - Sistemas Operativos II")

# Métricas operativas por réplica (reinician con el proceso; no alteran compras/BD).
_METRICAS_LOCK = Lock()
_METRICAS_INICIO = time.time()
_METRICAS = {"peticiones": 0, "errores_http": 0, "latencia_total_ms": 0.0, "latencia_max_ms": 0.0}
_METRICAS_RECIENTES = deque(maxlen=100)

class MiddlewareMetricas(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        inicio = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - inicio) * 1000
            with _METRICAS_LOCK:
                _METRICAS["peticiones"] += 1
                _METRICAS["errores_http"] += 1
                _METRICAS["latencia_total_ms"] += elapsed
                _METRICAS["latencia_max_ms"] = max(_METRICAS["latencia_max_ms"], elapsed)
                _METRICAS_RECIENTES.append({"timestamp": time.time(), "metodo": request.method, "ruta": request.url.path, "status": 500, "latencia_ms": round(elapsed, 2)})
            raise
        elapsed = (time.perf_counter() - inicio) * 1000
        with _METRICAS_LOCK:
            _METRICAS["peticiones"] += 1
            if response.status_code >= 400:
                _METRICAS["errores_http"] += 1
            _METRICAS["latencia_total_ms"] += elapsed
            _METRICAS["latencia_max_ms"] = max(_METRICAS["latencia_max_ms"], elapsed)
            _METRICAS_RECIENTES.append({"timestamp": time.time(), "metodo": request.method, "ruta": request.url.path, "status": response.status_code, "latencia_ms": round(elapsed, 2)})
        return response

app.add_middleware(MiddlewareMetricas)


FRONTEND_ORIGINS = [origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)

# URLs de los otros servicios (comunicacion entre procesos via REST/HTTP)
EVENTOS_URL = os.getenv("EVENTOS_URL", "http://eventos:8002")
# Identifica qué réplica atendió la petición ("1" o "2")
INSTANCE_ID = os.getenv("INSTANCE_ID", "1")
RECOVERY_STALE_SECONDS = max(30, int(os.getenv("PURCHASE_RECOVERY_STALE_SECONDS", "120")))
# Recuperación automática en segundo plano (0 = deshabilitada). Solo una
# réplica a la vez la ejecuta, coordinada con un advisory lock de PostgreSQL.
RECOVERY_INTERVAL_SECONDS = max(0, int(os.getenv("PURCHASE_RECOVERY_INTERVAL_SECONDS", "0")))
# Por defecto la recuperación automática solo avanza operaciones con compra
# persistida (confirmar reserva / reparar respuesta). Compensar (liberar
# stock) queda para /compras/recuperar-servidor salvo que se habilite aquí.
RECOVERY_AUTO_RELEASE = os.getenv("PURCHASE_RECOVERY_AUTO_RELEASE", "0") == "1"
RECOVERY_LOCK_KEY = 7419251032
# Espera máxima por el bloqueo de una Idempotency-Key que otra réplica procesa.
CLAIM_LOCK_TIMEOUT_MS = max(1000, int(os.getenv("PURCHASE_CLAIM_LOCK_TIMEOUT_MS", "15000")))
TEST_CRASH_HOOKS_ENABLED = (
    os.getenv("NEXUSTIX_TEST_CRASH_HOOKS", "0") == "1"
    and os.getenv("NEXUSTIX_DEPLOYMENT_ENV") == "staging"
)
# Por debajo de idle_in_transaction_session_timeout (120 s) de PostgreSQL.
TEST_HOLD_SECONDS = max(1, min(90, int(os.getenv("NEXUSTIX_TEST_HOLD_SECONDS", "30"))))


def _test_marker(kind: str, checkpoint: str, operation_id: str | None) -> None:
    # Una línea en el log del contenedor antes de detener o pausar el proceso,
    # para que la prueba pueda demostrar qué checkpoint se alcanzó.
    print(f"NEXUSTIX_TEST_{kind} checkpoint={checkpoint} instancia={INSTANCE_ID} operacion_id={operation_id or '-'}",
          file=sys.stderr, flush=True)


def _test_crash_at(requested: str | None, checkpoint: str, operation_id: str | None = None,
                   hold_requested: str | None = None) -> None:
    """Test-only process crash or pause; inert unless explicitly enabled in an isolated test deployment.

    crash: the process exits immediately (Docker's restart policy decides what follows).
    hold: the request sleeps at the checkpoint so the test can kill the
    container from outside (``docker kill``) while the operation is in flight.
    """
    if not TEST_CRASH_HOOKS_ENABLED:
        return
    if hold_requested == checkpoint:
        _test_marker("HOLD", checkpoint, operation_id)
        time.sleep(TEST_HOLD_SECONDS)
    if requested == checkpoint:
        _test_marker("CRASH", checkpoint, operation_id)
        os._exit(86)

# ---------- Reloj Lógico de Lamport ----------
RELOJ_LAMPORT = 0
_RELOJ_LOCK = Lock()


def tic_lamport(t_recibido: int = 0) -> int:
    """Avanza el reloj lógico de Lamport: max(L_local, L_msg) + 1."""
    global RELOJ_LAMPORT
    # Los endpoints síncronos corren en un pool de hilos: el incremento debe ser atómico.
    with _RELOJ_LOCK:
        RELOJ_LAMPORT = max(RELOJ_LAMPORT, t_recibido) + 1
        return RELOJ_LAMPORT


# ---------- Variables de Simulación de Fallos & Tolerancia ----------
SIMULACION_APAGADO = False  # Simula máquina o servidor apagado por corte de energía
SIMULACION_CORTE_RED = False  # Simula paquete perdido o corte de red en tránsito
JOURNAL_TRANSACCIONES = []  # Caché local; la fuente persistente es journal_compras en PostgreSQL


def registrar_journal(tipo: str, mensaje: str, detalles: dict = None, t: int = None):
    """Persiste eventos funcionales en PostgreSQL compartido por las réplicas."""
    global JOURNAL_TRANSACCIONES
    reloj = t if t is not None else tic_lamport()
    detalles_seguro = detalles or {}
    entrada = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reloj_t": reloj,
        "instancia": INSTANCE_ID,
        "tipo": tipo,
        "mensaje": mensaje,
        "detalles": detalles_seguro,
    }
    # La operación de negocio no debe fallar solo porque la bitácora no esté disponible.
    # El error se deja en el log del contenedor y la caché local conserva evidencia temporal.
    db_journal = SessionLocal()
    try:
        registro = RegistroJournal(
            reloj_t=reloj, instancia=INSTANCE_ID, tipo=tipo,
            mensaje=mensaje, detalles=detalles_seguro,
        )
        db_journal.add(registro)
        db_journal.commit()
        db_journal.refresh(registro)
        entrada["id"] = registro.id
        if registro.timestamp:
            entrada["timestamp"] = registro.timestamp.isoformat()
    except Exception as exc:
        db_journal.rollback()
        entrada["persistencia"] = "fallida"
        entrada["error_persistencia"] = str(exc)[:300]
    finally:
        db_journal.close()
    JOURNAL_TRANSACCIONES.append(entrada)
    if len(JOURNAL_TRANSACCIONES) > 100:
        JOURNAL_TRANSACCIONES.pop(0)
    return entrada


# El stock inicial se consulta al servicio Eventos, que es su fuente de verdad.


# ---------- Elección de Líder (Algoritmo del Matón / Bully Algorithm) ----------
PRIORIDAD_NODO = 100 if INSTANCE_ID == "1" else 50
ESTADO_LIDER = {
    "instancia": INSTANCE_ID,
    "es_lider": (INSTANCE_ID == "1"),
    "prioridad": PRIORIDAD_NODO,
    "estado_recursos": "optimo",  # "optimo" | "deteriorado_por_falta_de_recursos"
    "motivo": (
        "Nodo principal de mayor jerarquía activa y recursos óptimos (Algoritmo del Matón)"
        if INSTANCE_ID == "1"
        else "Nodo secundario / standby a la espera de relevo"
    ),
    "algoritmo": "Algoritmo del Matón (Bully Algorithm)",
}


# ---------- Modelos de Petición ----------
class CompraRequest(BaseModel):
    model_config = {"extra": "forbid"}
    evento_id: int = Field(gt=0)
    cantidad: int = Field(default=1, ge=1, le=1000)
    simular_fallo: bool = False
    tipo_imprevisto: str = "ninguno"  # "ninguno" | "apagon_maquina" | "corte_red" | "fallo_db"
    reloj_t: int = Field(default=0, ge=0)


class ItemTransaccionGrupal(BaseModel):
    evento_id: int = Field(gt=0)
    cantidad: int = Field(default=1, ge=1, le=1000)


class TransaccionGrupalRequest(BaseModel):
    model_config = {"extra": "forbid"}
    items: List[ItemTransaccionGrupal] = Field(min_length=1, max_length=20)
    simular_fallo: bool = False
    tipo_imprevisto: str = "ninguno"  # "ninguno" | "apagon_maquina" | "corte_red" | "fallo_db"
    reloj_t: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class Principal:
    user_id: int
    role: str
    subject: str


def get_principal(authorization: str | None = Header(default=None)) -> Principal:
    claims = authenticated_claims(authorization)
    return Principal(
        user_id=int(claims["sub"]),
        role=claims["role"],
        subject=f"{claims['iss']}|{claims['sub']}",
    )


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != "administrador":
        raise HTTPException(status_code=403, detail="Se requiere rol administrador")
    return principal


def _purchase_request_hash(datos: CompraRequest) -> str:
    canonical = json.dumps(datos.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _claim_purchase_idempotency(
    db: Session, key: str, principal: Principal, request_hash: str,
) -> tuple[IdempotencyRequest | None, JSONResponse | None]:
    if not key or len(key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise HTTPException(status_code=400, detail="Idempotency-Key debe contener entre 1 y 128 caracteres ASCII visibles")

    record = IdempotencyRequest(
        idempotency_key=key,
        auth_subject=principal.subject,
        request_hash=request_hash,
        operacion_id=uuid4().hex,
        state="processing",
    )
    db.add(record)
    try:
        # Persistir primero la clave y el operacion_id. Si una réplica muere
        # después de reservar en Eventos, otra puede continuar la misma saga.
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
    except Exception:
        db.rollback()
        raise

    # FOR UPDATE serializa compradores que llegan por réplicas distintas.
    # En PostgreSQL, un duplicado espera al dueño actual del lock; si el dueño
    # cae, el lock se libera y el duplicado retoma el mismo operacion_id.
    existing_query = db.query(IdempotencyRequest).filter(
        IdempotencyRequest.idempotency_key == key
    )
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores SELECT FOR UPDATE. Its database-level no-op UPDATE is
        # only a test-adapter lock; production coordination is PostgreSQL row
        # locking and the unique key constraint.
        existing = existing_query.first()
        if existing is not None:
            db.query(IdempotencyRequest).filter(
                IdempotencyRequest.idempotency_key == key
            ).update({"state": IdempotencyRequest.state}, synchronize_session=False)
            db.refresh(existing)
    else:
        # Acotar la espera: si otra réplica sigue procesando esta misma clave
        # (p. ej. un reintento del balanceador), responder 503 reintentable en
        # lugar de bloquear indefinidamente la petición.
        db.execute(text(f"SET LOCAL lock_timeout = '{CLAIM_LOCK_TIMEOUT_MS}ms'"))
        try:
            existing = existing_query.with_for_update().first()
        except OperationalError as exc:
            original = getattr(exc, "orig", None)
            db.rollback()
            if (getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)) == "55P03":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "mensaje": "Otra réplica está procesando esta misma Idempotency-Key; reintenta con la misma clave.",
                        "en_proceso": True,
                    },
                    headers={"Retry-After": "2"},
                ) from exc
            raise
    if existing is None:
        raise HTTPException(status_code=503, detail="No se pudo resolver la clave idempotente; reintenta con la misma clave")
    if existing.auth_subject != principal.subject or existing.request_hash != request_hash:
        db.rollback()
        raise HTTPException(status_code=409, detail="Idempotency-Key ya fue utilizada con otro usuario o contenido")
    if existing.state == "completed" and existing.response_status is not None and existing.response_body is not None:
        response = JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body,
            headers={"Idempotency-Replayed": "true"},
        )
        db.rollback()
        return None, response
    return existing, None


def _complete_idempotency(record: IdempotencyRequest, status_code: int, body: dict) -> None:
    record.state = "completed"
    record.response_status = status_code
    record.response_body = body


def _claim_is_stale(record: IdempotencyRequest, now: datetime | None = None) -> bool:
    created_at = record.created_at
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return created_at <= current - timedelta(seconds=RECOVERY_STALE_SECONDS)


def _purchase_replay_body(record: IdempotencyRequest | None, purchase: Compra) -> dict:
    body = dict(record.response_body or {}) if record and isinstance(record.response_body, dict) else {}
    body.update({
        "id": purchase.id,
        "operacion_id": purchase.operacion_id,
        "confirmacion_reserva": "confirmada",
        "usuario_id": purchase.usuario_id,
        "evento_id": purchase.evento_id,
        "cantidad": purchase.cantidad,
        "estado": purchase.estado,
    })
    body.setdefault("atendido_por", f"compras (instancia {purchase.instancia or 'recuperada'})")
    return body


def _try_lock_recovery_record(db: Session, operacion_id: str) -> tuple[IdempotencyRequest | None, bool]:
    try:
        record = db.query(IdempotencyRequest).filter(
            IdempotencyRequest.operacion_id == operacion_id
        ).with_for_update(nowait=True).first()
        return record, False
    except OperationalError as exc:
        db.rollback()
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
        sqlite_lock = db.get_bind().dialect.name == "sqlite" and "locked" in str(exc).lower()
        if sqlstate == "55P03" or sqlite_lock:
            return None, True
        # A database outage or unrelated SQL error must not be misreported as
        # a live purchase; let the endpoint surface the operational failure.
        raise


def eventos_headers() -> dict[str, str]:
    try:
        return service_auth_headers()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Autenticación con Eventos no configurada") from exc


class EventsResponseUnverifiable(ValueError):
    """Remote response cannot prove the exact durable reservation state."""


def _events_json_payload(response, context: str):
    try:
        return response.json()
    except ValueError as exc:
        raise EventsResponseUnverifiable(f"JSON inválido de Eventos ({context})") from exc


def _events_json_object(response, context: str) -> dict:
    payload = _events_json_payload(response, context)
    if not isinstance(payload, dict):
        raise EventsResponseUnverifiable(f"Objeto inválido de Eventos ({context})")
    return payload


def _events_int(payload: dict, field: str, *, minimum: int = 0) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EventsResponseUnverifiable("Campos numéricos inválidos de Eventos")
    return value


def _validate_reservation_identity(payload: dict, operation_id: str, event_id: int,
                                   quantity: int, *, state_field: str,
                                   allowed_states: set[str]) -> dict:
    if payload.get("operacion_id") != operation_id:
        raise EventsResponseUnverifiable("Operación distinta en respuesta de Eventos")
    if _events_int(payload, "evento_id", minimum=1) != event_id:
        raise EventsResponseUnverifiable("Evento distinto en respuesta de Eventos")
    if _events_int(payload, "cantidad", minimum=1) != quantity:
        raise EventsResponseUnverifiable("Cantidad distinta en respuesta de Eventos")
    if payload.get(state_field) not in allowed_states:
        raise EventsResponseUnverifiable("Estado inesperado en respuesta de Eventos")
    return payload


def _validate_reserve_payload(payload: dict, operation_id: str, event_id: int,
                              quantity: int) -> dict:
    _validate_reservation_identity(
        payload, operation_id, event_id, quantity, state_field="estado_reserva",
        allowed_states={"reservada"},
    )
    _events_int(payload, "stock_restante")
    if type(payload.get("idempotente")) is not bool:
        raise EventsResponseUnverifiable("Respuesta de reserva incompleta")
    _events_int(payload, "reloj_logico_T")
    return payload


def _validate_action_payload(payload: dict, action: str, operation_id: str,
                             event_id: int, quantity: int) -> dict:
    expected = "confirmada" if action == "confirmar" else "liberada"
    _validate_reservation_identity(
        payload, operation_id, event_id, quantity, state_field="estado_reserva",
        allowed_states={expected},
    )
    if action == "confirmar":
        if payload.get("status") != "ok" or type(payload.get("idempotente")) is not bool:
            raise EventsResponseUnverifiable("Respuesta de confirmación incompleta")
    else:
        _events_int(payload, "stock_restante")
        if type(payload.get("idempotente")) is not bool:
            raise EventsResponseUnverifiable("Respuesta de liberación incompleta")
    _events_int(payload, "reloj_logico_T")
    return payload


def _read_durable_reservation(operation_id: str, event_id: int,
                              quantity: int) -> dict | None:
    try:
        response = requests.get(
            f"{EVENTOS_URL}/eventos/reservas/{operation_id}",
            headers=eventos_headers(), timeout=5,
        )
    except (requests.exceptions.RequestException, HTTPException) as exc:
        raise EventsResponseUnverifiable("No se pudo consultar el estado durable de Eventos") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise EventsResponseUnverifiable("Eventos no pudo verificar el estado durable")
    payload = _events_json_object(response, "consulta durable")
    _validate_reservation_identity(
        payload, operation_id, event_id, quantity, state_field="estado",
        allowed_states={"reservada", "confirmada", "liberada"},
    )
    _events_int(payload, "stock_actual")
    return payload


def _resolve_reservation(response, operation_id: str, event_id: int,
                         quantity: int, *, transport_failed: bool = False) -> dict:
    if not transport_failed and response is not None and response.status_code == 200:
        try:
            return _validate_reserve_payload(
                _events_json_object(response, "reserva"), operation_id, event_id, quantity,
            )
        except EventsResponseUnverifiable:
            pass
    durable = _read_durable_reservation(operation_id, event_id, quantity)
    if durable is not None and durable["estado"] == "reservada":
        return {
            "evento_id": event_id, "cantidad": quantity, "operacion_id": operation_id,
            "estado_reserva": "reservada", "stock_restante": durable["stock_actual"],
            "idempotente": True,
        }
    raise EventsResponseUnverifiable("No hay evidencia durable suficiente para continuar la compra")


def _perform_event_action(action: str, operation_id: str, event_id: int,
                          quantity: int, logical_clock: int) -> dict:
    try:
        response = requests.post(
            f"{EVENTOS_URL}/eventos/{event_id}/{action}",
            params={"cantidad": quantity, "reloj_t": logical_clock,
                    "operacion_id": operation_id},
            headers=eventos_headers(), timeout=5,
        )
        if response.status_code == 200:
            try:
                return _validate_action_payload(
                    _events_json_object(response, action), action, operation_id,
                    event_id, quantity,
                )
            except EventsResponseUnverifiable:
                pass
    except (requests.exceptions.RequestException, HTTPException):
        pass
    durable = _read_durable_reservation(operation_id, event_id, quantity)
    expected = "confirmada" if action == "confirmar" else "liberada"
    if durable is not None and durable["estado"] == expected:
        return {
            **durable,
            "estado_reserva": expected,
            "stock_restante": durable["stock_actual"],
            "idempotente": True,
        }
    raise EventsResponseUnverifiable("No se pudo verificar el resultado durable de Eventos")


def _validate_pending_reservations(payload: dict) -> list[dict]:
    rows = payload.get("reservas")
    total = payload.get("total")
    if not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int) or total != len(rows):
        raise EventsResponseUnverifiable("Lista de reservas pendientes inválida")
    validated = []
    for row in rows:
        if not isinstance(row, dict):
            raise EventsResponseUnverifiable("Reserva pendiente inválida")
        operation_id = row.get("operacion_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise EventsResponseUnverifiable("Reserva pendiente sin operación")
        _events_int(row, "evento_id", minimum=1)
        _events_int(row, "cantidad", minimum=1)
        if row.get("estado") != "reservada":
            raise EventsResponseUnverifiable("Estado pendiente inválido")
        validated.append(row)
    return validated


def _validate_stock_audit(payload) -> list[dict]:
    if not isinstance(payload, list):
        raise EventsResponseUnverifiable("Lista de auditoría de stock inválida")
    for event in payload:
        if not isinstance(event, dict):
            raise EventsResponseUnverifiable("Evento inválido en auditoría remota")
        _events_int(event, "id", minimum=1)
        if not isinstance(event.get("nombre"), str):
            raise EventsResponseUnverifiable("Nombre de evento inválido en auditoría remota")
        _events_int(event, "stock_actual")
        initial_stock = event.get("stock_inicial")
        if initial_stock is not None and (
            isinstance(initial_stock, bool) or not isinstance(initial_stock, int) or initial_stock < 0
        ):
            raise EventsResponseUnverifiable("Stock inicial inválido en auditoría remota")
        operations = event.get("operaciones_confirmadas")
        if not isinstance(operations, list):
            raise EventsResponseUnverifiable("Operaciones confirmadas inválidas en auditoría remota")
        for operation in operations:
            if not isinstance(operation, dict):
                raise EventsResponseUnverifiable("Operación confirmada inválida en auditoría remota")
            if not isinstance(operation.get("operacion_id"), str) or not operation["operacion_id"]:
                raise EventsResponseUnverifiable("Operación confirmada sin identificador")
            _events_int(operation, "cantidad", minimum=1)
    return payload


def _ambiguous_purchase_response(db: Session, operation_id: str) -> JSONResponse:
    db.rollback()
    return JSONResponse(status_code=503, content={"detail": {
        "mensaje": "No se pudo verificar la reserva en Eventos; la operación queda pendiente de retry o recuperación.",
        "operacion_id": operation_id,
        "recuperacion_requerida": True,
    }})


def require_admin_for_simulation(datos, principal: Principal) -> None:
    if (datos.simular_fallo or datos.tipo_imprevisto != "ninguno") and principal.role != "administrador":
        raise HTTPException(status_code=403, detail="Las simulaciones técnicas requieren rol administrador")


AUTH_MIGRATION_REVISION = "20261102_03"


def esperar_bd():
    intentos = 10
    while intentos:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            if revision != AUTH_MIGRATION_REVISION:
                raise RuntimeError("La base de Compras requiere la migración de autenticación aprobada")
            return
        except ProgrammingError as exc:
            # Sin tabla alembic_version: base sin migrar. Reintentar no lo arregla.
            raise RuntimeError(
                "La base de Compras no tiene esquema migrado; ejecute 'python -m shared.run_migrations'"
            ) from exc
        except OperationalError:
            intentos -= 1
            time.sleep(3)
    raise RuntimeError("No se pudo conectar a la base de datos de compras")


@app.on_event("startup")
def startup():
    esperar_bd()
    registrar_journal(
        "INFO",
        f"Servicio Compras instancia {INSTANCE_ID} iniciado correctamente.",
        {"prioridad": PRIORIDAD_NODO, "es_lider": ESTADO_LIDER["es_lider"]},
    )
    if RECOVERY_INTERVAL_SECONDS and engine.dialect.name == "postgresql":
        _iniciar_recuperacion_automatica()


@app.on_event("shutdown")
def shutdown():
    _RECUPERACION_STOP.set()


def validar_nodo_disponible():
    """Si el nodo fue apagado abruptamente por simulación, rechaza con HTTP 503 para activar Failover en Nginx."""
    if SIMULACION_APAGADO:
        raise HTTPException(
            status_code=503,
            detail={
                "mensaje": f"Servidor Compras (instancia {INSTANCE_ID}) está APAGADO ABRUPTAMENTE.",
                "instancia": INSTANCE_ID,
                "estado": "apagado_simulado",
                "failover": "El balanceador Nginx redirige el tráfico a la réplica superviviente.",
            },
        )


# =========================================================================
# RUTAS DE CONTROL DE SALUD Y FAILOVER
# =========================================================================
@app.get("/metricas")
def obtener_metricas():
    """Métricas de observabilidad locales a esta réplica; no modifica el estado de negocio."""
    with _METRICAS_LOCK:
        m = dict(_METRICAS)
        recientes = list(_METRICAS_RECIENTES)[-20:]
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            memoria = {line.split(":", 1)[0]: int(line.split()[1]) for line in f if ":" in line}
        total_kb = memoria.get("MemTotal", 0)
        disponible_kb = memoria.get("MemAvailable", 0)
        memoria_pct = round((total_kb - disponible_kb) / total_kb * 100, 2) if total_kb else None
    except (OSError, ValueError, IndexError):
        memoria_pct = None
    return {
        "servicio": "compras", "instancia": INSTANCE_ID,
        "uptime_segundos": round(time.time() - _METRICAS_INICIO, 2),
        "peticiones_totales": m["peticiones"], "errores_http": m["errores_http"],
        "tasa_errores_pct": round(m["errores_http"] / m["peticiones"] * 100, 2) if m["peticiones"] else 0.0,
        "latencia_promedio_ms": round(m["latencia_total_ms"] / m["peticiones"], 2) if m["peticiones"] else 0.0,
        "latencia_maxima_ms": round(m["latencia_max_ms"], 2),
        "memoria_host_contenedor_pct": memoria_pct,
        "lider": ESTADO_LIDER["es_lider"], "prioridad": PRIORIDAD_NODO,
        "reloj_logico_T": RELOJ_LAMPORT, "eventos_recientes": recientes,
        "nota": "Contadores locales al proceso; se reinician al reiniciar la réplica. La memoria refleja /proc del contenedor/host según su aislamiento."
    }


@app.get("/health")
def health(response: Response, db: Session = Depends(get_db)):
    if SIMULACION_APAGADO:
        response.status_code = 503
        return {
            "status": "down",
            "servicio": "compras",
            "instancia": INSTANCE_ID,
            "motivo": "Máquina apagada abruptamente (simulación)",
            "es_lider": False,
        }

    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.rollback()
        response.status_code = 503
        return {
            "status": "unavailable",
            "servicio": "compras",
            "instancia": INSTANCE_ID,
            "db": "unavailable",
        }

    return {
        "status": "ok",
        "servicio": "compras",
        "instancia": INSTANCE_ID,
        "db": "connected",
        "lider": ESTADO_LIDER["es_lider"],
        "estado_recursos": ESTADO_LIDER["estado_recursos"],
        "reloj_logico_T": tic_lamport(),
        "timestamp_fisico": time.time(),
        "apagado": SIMULACION_APAGADO,
        "corte_red": SIMULACION_CORTE_RED,
        "recuperacion_automatica": _estado_recuperacion_automatica(),
    }


# =========================================================================
# RUTAS DE SIMULACIÓN DE IMPREVISTOS Y RECUPERACIÓN (CRASH RECOVERY)
# =========================================================================
@app.post("/simulador/apagar")
def simular_apagon(_admin: Principal = Depends(require_admin)):
    """
    Simula apagar la máquina o proceso abruptamente (corte de energía).
    Al responder con 503, Nginx conmuta inmediatamente a la réplica activa.
    """
    global SIMULACION_APAGADO
    SIMULACION_APAGADO = True
    t = tic_lamport()
    registrar_journal(
        "APAGON",
        f"🚨 Servidor Compras {INSTANCE_ID} apagado abruptamente por corte de energía simulado.",
        {"instancia": INSTANCE_ID},
        t,
    )
    return {
        "status": "apagado",
        "instancia": INSTANCE_ID,
        "mensaje": f"Instancia {INSTANCE_ID} apagada. Peticiones futuras recibirán HTTP 503 activando Failover.",
        "reloj_logico_T": t,
    }


@app.post("/simulador/encender")
def simular_encendido(db: Session = Depends(get_db), _admin: Principal = Depends(require_admin)):
    """Reenciende el servidor y valida la recuperación de datos persistidos."""
    global SIMULACION_APAGADO
    SIMULACION_APAGADO = False
    t = tic_lamport()
    total_compras = db.query(Compra).count()
    registrar_journal(
        "RECOVERY",
        f"Instancia {INSTANCE_ID} reactivada; se encontraron {total_compras} filas de compra. La consistencia debe comprobarse con la auditoría.",
        {"instancia": INSTANCE_ID, "compras_encontradas": total_compras},
        t,
    )
    return {
        "status": "activo",
        "instancia": INSTANCE_ID,
        "compras_encontradas": total_compras,
        "mensaje": "Instancia reactivada. Este conteo no certifica por sí solo la integridad; ejecute /compras/recuperar-servidor para reconciliar reservas y auditar datos.",
        "reloj_logico_T": t,
    }


@app.get("/simulador/estado")
def obtener_estado_simulador():
    return {
        "instancia": INSTANCE_ID,
        "apagado": SIMULACION_APAGADO,
        "corte_red": SIMULACION_CORTE_RED,
        "es_lider": ESTADO_LIDER["es_lider"],
    }


@app.post("/compras/recuperar-servidor")
def recuperar_servidor(db: Session = Depends(get_db), _admin: Principal = Depends(require_admin)):
    """
    Reconcilia reservas y respuestas ambiguas solo con evidencia de ambas bases.
    Antigüedad sirve para seleccionar claims processing, nunca para liberar stock.
    """
    global SIMULACION_APAGADO, SIMULACION_CORTE_RED
    SIMULACION_APAGADO = False
    SIMULACION_CORTE_RED = False
    return ejecutar_recuperacion(db, permitir_compensacion=True, incluir_auditoria=True, origen="admin")


def ejecutar_recuperacion(db: Session, *, permitir_compensacion: bool, incluir_auditoria: bool,
                          origen: str) -> dict:
    """Protocolo de recuperación compartido por el endpoint admin y el proceso automático.

    permitir_compensacion=False limita la ejecución a avanzar operaciones cuya
    compra ya está persistida (confirmar la reserva y reparar el replay); las
    reservas sin compra quedan intactas y se informan para revisión.
    """
    t = tic_lamport()
    compras = db.query(Compra).filter(Compra.estado == "confirmada").all()
    counters = {
        "reservas_confirmadas": 0,
        "reservas_liberadas": 0,
        "replays_actualizados": 0,
        "claims_sin_reserva": 0,
        "operaciones_ambiguas": 0,
        "omitidas_en_proceso": 0,
        "compensaciones_requieren_admin": 0,
    }
    diagnostics: list[dict] = []

    try:
        r_pend = requests.get(
            f"{EVENTOS_URL}/eventos/reservas/pendientes",
            headers=eventos_headers(),
            timeout=5,
        )
        if r_pend.status_code != 200:
            raise EventsResponseUnverifiable("Eventos no pudo listar las reservas pendientes")
        pendientes = _validate_pending_reservations(_events_json_object(r_pend, "reservas pendientes"))
    except (requests.exceptions.RequestException, EventsResponseUnverifiable, HTTPException) as exc:
        db.rollback()
        return {
            "status": "recuperacion_incompleta",
            "instancia": INSTANCE_ID,
            "origen": origen,
            "reservas_consultadas": False,
            **counters,
            "contadores": counters,
            "diagnosticos": [{"tipo": "eventos_no_verificable", "detalle": str(exc)}],
            "integridad_base_datos": "no_verificable",
            "auditoria_consistencia": None,
            "mensaje": "No se pudo leer Eventos; no se modificó ninguna reserva.",
            "reloj_logico_T": t,
        }

    # Include old claims that may never have created an Events reservation, plus
    # completed purchases whose durable replay still says confirmation pending.
    candidate_ids = {str(row.get("operacion_id")) for row in pendientes if row.get("operacion_id")}
    now = datetime.now(timezone.utc)
    processing = db.query(IdempotencyRequest).filter(IdempotencyRequest.state == "processing").all()
    for record in processing:
        if _claim_is_stale(record, now):
            candidate_ids.add(record.operacion_id)
    completed = db.query(IdempotencyRequest).filter(IdempotencyRequest.state == "completed").all()
    pending_replay_ids = {
        record.operacion_id for record in completed
        if isinstance(record.response_body, dict)
        and record.response_body.get("confirmacion_reserva") == "pendiente"
    }
    candidate_ids.update(pending_replay_ids)

    pending_ids = candidate_ids.copy()
    for op_id in sorted(candidate_ids):
        if op_id.startswith("ord-"):
            continue  # El coordinador de pedidos posee su propia fila durable y expiración.
        record, active = _try_lock_recovery_record(db, op_id)
        if active:
            counters["omitidas_en_proceso"] += 1
            diagnostics.append({"operacion_id": op_id, "tipo": "operacion_activa", "accion": "omitida_sin_cambios"})
            continue

        # Re-evaluate after acquiring the row lock; the initial scan is only a
        # candidate list and can be stale by the time a recovery worker gets here.
        if record is not None and record.state == "processing" and not _claim_is_stale(record, now):
            db.rollback()
            diagnostics.append({"operacion_id": op_id, "tipo": "processing_reciente", "accion": "sin_cambios"})
            continue

        purchase = db.query(Compra).filter(
            Compra.operacion_id == op_id, Compra.estado == "confirmada"
        ).first()
        try:
            external = _read_durable_reservation(op_id,
                purchase.evento_id if purchase else next(
                    (row["evento_id"] for row in pendientes if row["operacion_id"] == op_id), 1
                ),
                purchase.cantidad if purchase else next(
                    (row["cantidad"] for row in pendientes if row["operacion_id"] == op_id), 1
                ),
            )
        except EventsResponseUnverifiable as exc:
            db.rollback()
            counters["operaciones_ambiguas"] += 1
            diagnostics.append({"operacion_id": op_id, "tipo": "estado_eventos_no_verificable", "accion": "sin_cambios"})
            continue

        if external is None:
            db.rollback()
            if purchase:
                counters["operaciones_ambiguas"] += 1
                kind = "compra_sin_reserva_eventos"
            elif record is not None and record.state == "processing":
                counters["claims_sin_reserva"] += 1
                kind = "claim_sin_reserva_reintentable"
            else:
                counters["operaciones_ambiguas"] += 1
                kind = "reserva_desaparecida"
            diagnostics.append({"operacion_id": op_id, "tipo": kind, "accion": "sin_cambios"})
            continue
        state = external["estado"]
        event_id = external["evento_id"]
        quantity = external["cantidad"]

        if purchase and (purchase.evento_id != event_id or purchase.cantidad != quantity):
            db.rollback()
            counters["operaciones_ambiguas"] += 1
            diagnostics.append({"operacion_id": op_id, "tipo": "compra_reserva_no_coinciden", "accion": "sin_cambios"})
            continue

        if state == "confirmada":
            if not purchase:
                db.rollback()
                counters["operaciones_ambiguas"] += 1
                diagnostics.append({"operacion_id": op_id, "tipo": "reserva_confirmada_sin_compra", "accion": "requiere_revision"})
                continue
            if record is not None:
                record.state = "completed"
                record.response_status = 200
                record.response_body = _purchase_replay_body(record, purchase)
                try:
                    db.commit()
                    counters["replays_actualizados"] += 1
                except SQLAlchemyError as exc:
                    db.rollback()
                    counters["operaciones_ambiguas"] += 1
                    diagnostics.append({"operacion_id": op_id, "tipo": "persistencia_replay_fallida", "detalle": str(exc), "accion": "reintentar_recovery"})
                    continue
            else:
                db.rollback()
            counters["reservas_confirmadas"] += 1
            continue

        if state == "reservada":
            if purchase:
                action = "confirmar"
            elif record is not None and record.state == "processing" and _claim_is_stale(record, now):
                action = "liberar"
            else:
                db.rollback()
                counters["operaciones_ambiguas"] += 1
                diagnostics.append({"operacion_id": op_id, "tipo": "reserva_sin_compra_no_abandonada", "accion": "sin_cambios"})
                continue
        elif state == "liberada":
            if purchase:
                db.rollback()
                counters["operaciones_ambiguas"] += 1
                diagnostics.append({"operacion_id": op_id, "tipo": "compra_con_reserva_liberada", "accion": "requiere_revision"})
                continue
            if record is None or record.state != "processing" or not _claim_is_stale(record, now):
                db.rollback()
                diagnostics.append({"operacion_id": op_id, "tipo": "reserva_liberada_sin_compra", "accion": "sin_cambios"})
                continue
            action = "finalizar_fallo"
        else:
            db.rollback()
            counters["operaciones_ambiguas"] += 1
            diagnostics.append({"operacion_id": op_id, "tipo": "estado_eventos_desconocido", "accion": "sin_cambios"})
            continue

        if action == "liberar" and not permitir_compensacion:
            # La compensación devuelve stock: en modo automático se deja la
            # reserva intacta y la clave reanudable hasta que un administrador
            # ejecute /compras/recuperar-servidor o el cliente reintente.
            db.rollback()
            counters["compensaciones_requieren_admin"] += 1
            diagnostics.append({"operacion_id": op_id, "tipo": "compensacion_requiere_admin", "accion": "sin_cambios"})
            continue

        if action in {"confirmar", "liberar"}:
            try:
                _perform_event_action(action, op_id, event_id, quantity, tic_lamport())
            except EventsResponseUnverifiable as exc:
                db.rollback()
                counters["operaciones_ambiguas"] += 1
                diagnostics.append({"operacion_id": op_id, "tipo": "accion_eventos_ambigua", "accion": "sin_cambios_locales"})
                continue
            if action == "confirmar":
                counters["reservas_confirmadas"] += 1
                if record is not None:
                    record.state = "completed"
                    record.response_status = 200
                    record.response_body = _purchase_replay_body(record, purchase)
                    try:
                        db.commit()
                        counters["replays_actualizados"] += 1
                    except SQLAlchemyError as exc:
                        db.rollback()
                        counters["operaciones_ambiguas"] += 1
                        diagnostics.append({"operacion_id": op_id, "tipo": "persistencia_replay_fallida", "detalle": str(exc), "accion": "reintentar_recovery"})
                else:
                    db.rollback()
            else:
                counters["reservas_liberadas"] += 1
                if record is not None:
                    record.state = "completed"
                    record.response_status = 409
                    record.response_body = {"detail": {
                        "mensaje": "La operación interrumpida fue compensada; no se creó una compra.",
                        "operacion_id": op_id,
                        "estado_reserva": "liberada",
                        "recuperacion": "completada",
                    }}
                    try:
                        db.commit()
                    except SQLAlchemyError as exc:
                        db.rollback()
                        counters["operaciones_ambiguas"] += 1
                        diagnostics.append({"operacion_id": op_id, "tipo": "persistencia_compensacion_fallida", "detalle": str(exc), "accion": "reintentar_recovery"})
                else:
                    db.rollback()
            continue

        if action == "finalizar_fallo" and record is not None:
            record.state = "completed"
            record.response_status = 409
            record.response_body = {"detail": {
                "mensaje": "La reserva ya estaba liberada y no existe compra; no se repitió la operación.",
                "operacion_id": op_id,
                "estado_reserva": "liberada",
                "recuperacion": "completada",
            }}
            try:
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                counters["operaciones_ambiguas"] += 1
                diagnostics.append({"operacion_id": op_id, "tipo": "persistencia_fallo_finalizada_fallida", "detalle": str(exc), "accion": "reintentar_recovery"})

    auditoria = None
    integridad = "no_auditada"
    if incluir_auditoria:
        try:
            auditoria = auditar_consistencia(db)
            if auditoria.get("todo_consistente") and auditoria.get("auditoria_completa"):
                integridad = "consistente"
            elif auditoria.get("todo_consistente"):
                integridad = "consistente_con_historial_legacy_no_correlacionado"
            else:
                integridad = "inconsistente"
        except HTTPException as exc:
            auditoria = {"todo_consistente": None, "detalle": exc.detail}
            integridad = "no_verificable"

    changed = counters["reservas_confirmadas"] or counters["reservas_liberadas"] or counters["replays_actualizados"]
    if origen == "admin" or changed:
        # El proceso automático solo deja traza cuando modificó algo, para no
        # llenar el journal con barridos vacíos.
        registrar_journal(
            "RECOVERY",
            "Recuperación de compras ambiguas ejecutada; revisar diagnósticos por operación.",
            {"origen": origen, "compras_confirmadas": len(compras), "contadores": counters,
             "diagnosticos": diagnostics, "integridad": integridad},
            t,
        )

    incomplete = bool(diagnostics) or counters["operaciones_ambiguas"] or counters["claims_sin_reserva"] or counters["omitidas_en_proceso"]
    return {
        "status": "recuperacion_incompleta" if incomplete else "recuperacion_completa",
        "instancia": INSTANCE_ID,
        "origen": origen,
        "reservas_consultadas": True,
        "compras_confirmadas_persistidas": len(compras),
        "ventana_claims_stale_segundos": RECOVERY_STALE_SECONDS,
        "contadores": counters,
        "diagnosticos": diagnostics,
        "integridad_base_datos": integridad,
        "auditoria_consistencia": auditoria,
        "protocolo": "Lock NOWAIT + compra local + consulta exacta de estado durable en Eventos",
        "reloj_logico_T": tic_lamport(),
    }


# ---------- Recuperación automática en segundo plano ----------
_RECUPERACION_STOP = Event()
_RECUPERACION_ESTADO = {"habilitada": False, "intervalo_s": RECOVERY_INTERVAL_SECONDS,
                        "compensacion_automatica": RECOVERY_AUTO_RELEASE,
                        "ultima_ejecucion": None, "ultimo_resultado": None, "ultimo_error": None}
_RECUPERACION_ESTADO_LOCK = Lock()


def _estado_recuperacion_automatica() -> dict:
    with _RECUPERACION_ESTADO_LOCK:
        return dict(_RECUPERACION_ESTADO)


def ejecutar_barrido_automatico() -> dict:
    """Un barrido: solo si esta réplica obtiene el advisory lock global.

    El lock vive en una conexión dedicada mientras dura el barrido; si el
    proceso muere, PostgreSQL lo libera al cerrarse la conexión.
    """
    if SIMULACION_APAGADO:
        return {"status": "omitido", "motivo": "nodo_apagado_simulado"}
    lock_connection = engine.connect()
    try:
        acquired = lock_connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": RECOVERY_LOCK_KEY}
        ).scalar()
        lock_connection.commit()
        if not acquired:
            return {"status": "omitido", "motivo": "otra_replica_ejecuta_la_recuperacion"}
        db = SessionLocal()
        try:
            result = ejecutar_recuperacion(
                db, permitir_compensacion=RECOVERY_AUTO_RELEASE,
                incluir_auditoria=False, origen=f"automatica_instancia_{INSTANCE_ID}",
            )
        finally:
            db.close()
            lock_connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RECOVERY_LOCK_KEY})
            lock_connection.commit()
        return {"status": result["status"], "contadores": result["contadores"]}
    finally:
        lock_connection.close()


def _bucle_recuperacion_automatica() -> None:
    while not _RECUPERACION_STOP.wait(RECOVERY_INTERVAL_SECONDS + random.uniform(0, 3)):
        try:
            resultado = ejecutar_barrido_automatico()
            error = None
        except Exception as exc:  # noqa: BLE001 - el hilo nunca debe morir por un fallo transitorio
            resultado, error = None, f"{type(exc).__name__}: {str(exc)[:200]}"
        with _RECUPERACION_ESTADO_LOCK:
            _RECUPERACION_ESTADO["ultima_ejecucion"] = datetime.now(timezone.utc).isoformat()
            _RECUPERACION_ESTADO["ultimo_resultado"] = resultado
            _RECUPERACION_ESTADO["ultimo_error"] = error


def _iniciar_recuperacion_automatica() -> None:
    with _RECUPERACION_ESTADO_LOCK:
        if _RECUPERACION_ESTADO["habilitada"]:
            return
        _RECUPERACION_ESTADO["habilitada"] = True
    Thread(target=_bucle_recuperacion_automatica, name="recuperacion-compras", daemon=True).start()


# =========================================================================
# RUTAS DE LIDERAZGO (BULLY ALGORITHM)
# =========================================================================
@app.get("/lider")
def obtener_estado_lider():
    return {
        **ESTADO_LIDER,
        "reloj_logico_T": tic_lamport(),
    }


@app.post("/lider/ceder")
def ceder_liderazgo(motivo: str = "Agotamiento de CPU/Memoria y degradación de recursos", _admin: Principal = Depends(require_admin)):
    ESTADO_LIDER["es_lider"] = False
    ESTADO_LIDER["estado_recursos"] = "deteriorado_por_falta_de_recursos"
    ESTADO_LIDER["motivo"] = f"Cede dirección voluntariamente: {motivo}"
    registrar_journal("LIDER", f"Instancia {INSTANCE_ID} cede liderazgo: {motivo}")
    return {
        **ESTADO_LIDER,
        "reloj_logico_T": tic_lamport(),
        "mensaje": "Líder anterior ha renunciado. Réplica debe asumir la dirección.",
    }


@app.post("/lider/asumir")
def asumir_liderazgo(_admin: Principal = Depends(require_admin)):
    ESTADO_LIDER["es_lider"] = True
    ESTADO_LIDER["estado_recursos"] = "optimo"
    ESTADO_LIDER["motivo"] = "Asume liderazgo por degradación o caída del principal (Bully Failover)"
    registrar_journal("LIDER", f"Instancia {INSTANCE_ID} asume como LÍDER PRINCIPAL")
    return {
        **ESTADO_LIDER,
        "reloj_logico_T": tic_lamport(),
        "mensaje": f"Instancia {INSTANCE_ID} es ahora el NUEVO LÍDER activo.",
    }


@app.post("/lider/restaurar")
def restaurar_liderazgo(_admin: Principal = Depends(require_admin)):
    ESTADO_LIDER["es_lider"] = (INSTANCE_ID == "1")
    ESTADO_LIDER["estado_recursos"] = "optimo"
    ESTADO_LIDER["motivo"] = (
        "Restaurado a condiciones normales"
        if INSTANCE_ID == "1"
        else "Nodo standby / seguidor"
    )
    return {
        **ESTADO_LIDER,
        "reloj_logico_T": tic_lamport(),
    }


# =========================================================================
# LISTADO E HISTORIAL DE COMPRAS
# =========================================================================
@app.get("/compras/mias")
def listar_mis_compras(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
    validar_nodo_disponible()
    compras = db.query(Compra).filter(Compra.auth_subject == principal.subject).order_by(Compra.id.desc()).all()
    return [{
        "id": c.id, "evento_id": c.evento_id, "cantidad": c.cantidad,
        "estado": c.estado, "creado_en": c.creado_en.isoformat() if c.creado_en else None,
    } for c in compras]


@app.get("/compras")
def listar_compras(db: Session = Depends(get_db), _admin: Principal = Depends(require_admin)):
    validar_nodo_disponible()
    compras = db.query(Compra).all()
    return [
        {
            "id": c.id,
            "usuario_id": c.usuario_id,
            "evento_id": c.evento_id,
            "cantidad": c.cantidad,
            "estado": c.estado,
            "instancia": c.instancia,
        }
        for c in compras
    ]

@app.get("/compras/journal")
def obtener_journal(db: Session = Depends(get_db), _admin: Principal = Depends(require_admin)):
    """Devuelve la bitácora persistente compartida por compras1 y compras2."""
    registros = db.query(RegistroJournal).order_by(RegistroJournal.id.desc()).limit(200).all()
    return {
        "total_entradas": db.query(RegistroJournal).count(),
        "instancia_consultada": INSTANCE_ID,
        "persistente": True,
        "entradas": [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "reloj_t": r.reloj_t,
                "instancia": r.instancia,
                "tipo": r.tipo,
                "mensaje": r.mensaje,
                "detalles": r.detalles or {},
            }
            for r in registros
        ],
        "reloj_logico_T": tic_lamport(),
    }


@app.get("/compras/{compra_id}")
def obtener_compra(compra_id: int, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
    validar_nodo_disponible()
    compra = db.query(Compra).filter(Compra.id == compra_id).first()
    if not compra:
        raise HTTPException(status_code=404, detail="Compra no encontrada")
    if principal.role != "administrador" and compra.auth_subject != principal.subject:
        raise HTTPException(status_code=404, detail="Compra no encontrada")
    return {
        "id": compra.id,
        "usuario_id": compra.usuario_id,
        "evento_id": compra.evento_id,
        "cantidad": compra.cantidad,
        "estado": compra.estado,
        "instancia": compra.instancia,
    }


# =========================================================================
# TRANSACCIÓN INDIVIDUAL CON RECUPERACIÓN A FALLOS E IMPREVISTOS
# =========================================================================
@app.post("/compras")
def crear_compra(
    datos: CompraRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
    test_crash_at: str | None = Header(default=None, alias="X-NEXUSTIX-Test-Crash-At"),
    test_hold_at: str | None = Header(default=None, alias="X-NEXUSTIX-Test-Hold-At"),
):
    """
    Procesa una compra individual aplicando:
    1. Comprobación de estado de máquina (Tolerancia / Failover).
    2. Simulación de imprevistos (Apagón forzado, corte de red, o fallo en commit).
    3. Reloj Lógico de Lamport para orden causal.
    4. Transacción compensatoria SAGA si ocurre un imprevisto.
    5. Preservación íntegra de datos previamente guardados.
    """
    require_admin_for_simulation(datos, principal)
    validar_nodo_disponible()
    _test_crash_at(test_crash_at, "before_claim", None, test_hold_at)
    idempotency_record, replay = _claim_purchase_idempotency(
        db, idempotency_key, principal, _purchase_request_hash(datos),
    )
    if replay is not None:
        return replay
    assert idempotency_record is not None
    _test_crash_at(test_crash_at, "after_claim", idempotency_record.operacion_id, test_hold_at)

    dialogo_consenso = []
    t_inicio_proceso = tic_lamport(datos.reloj_t)
    tipo_imprevisto = datos.tipo_imprevisto or ("fallo_db" if datos.simular_fallo else "ninguno")

    if tipo_imprevisto == "corte_red":
        t_err = tic_lamport()
        dialogo_consenso.append({
            "paso": 1,
            "servidor": "red_distribuida",
            "fase": "TRANSPORTE",
            "accion": "Transmisión de mensaje hacia 'usuarios'",
            "respuesta": "PAQUETE_PERDIDO (Corte de red simulado: la transacción no llega)",
            "cumplio_secuencia": False,
            "reloj_logico_T": t_err,
        })
        registrar_journal(
            "CORTE_RED",
            "Transacción abortada: el mensaje no llegó al servidor por corte de red simulado.",
            {"usuario_id": principal.user_id, "evento_id": datos.evento_id},
            t_err,
        )
        failure = {
                "mensaje": "Corte de red simulado: el paquete se perdió en tránsito y la transacción no llegó al servidor.",
                "decision_consenso": "GLOBAL_ABORT",
                "imprevisto": "corte_red",
                "recuperacion": "No se realizaron cargos ni reservas. Los datos previos se mantienen constantes e íntegros.",
                "dialogo_consenso": dialogo_consenso,
            }
        _complete_idempotency(idempotency_record, 504, {"detail": failure})
        db.commit()
        return JSONResponse(status_code=504, content={"detail": failure})

    dialogo_consenso.append({
        "paso": 1,
        "servidor": "compras",
        "fase": "AUTENTICACION",
        "accion": "Validar subject y rol del token firmado por Usuarios",
        "respuesta": "VOTO_AFIRMATIVO (identidad autenticada)",
        "cumplio_secuencia": True,
        "reloj_logico_T": tic_lamport(),
    })

    # Esta fila queda sin confirmar mientras se procesa: su restricción única
    # coordina ambas réplicas y se confirma atómicamente con la compra/respuesta.
    # Si el proceso cae después de reservar en Eventos, la recuperación libera
    # esa reserva huérfana; un reintento posterior podrá reclamar la misma clave.
    operacion_id = idempotency_record.operacion_id

    # Paso 2: Reserva con Exclusión Mutua en Eventos
    t_msg_eventos = tic_lamport()
    resp_reserva = None
    reserva_data = None
    try:
        resp_reserva = requests.post(
            f"{EVENTOS_URL}/eventos/{datos.evento_id}/reservar",
            params={"cantidad": datos.cantidad, "reloj_t": t_msg_eventos, "operacion_id": operacion_id},
            headers=eventos_headers(),
            timeout=5,
        )
    except requests.exceptions.RequestException:
        try:
            reserva_data = _resolve_reservation(
                None, operacion_id, datos.evento_id, datos.cantidad, transport_failed=True,
            )
        except EventsResponseUnverifiable:
            return _ambiguous_purchase_response(db, operacion_id)

    if reserva_data is None and resp_reserva is not None and resp_reserva.status_code >= 500:
        try:
            reserva_data = _resolve_reservation(resp_reserva, operacion_id, datos.evento_id, datos.cantidad)
        except EventsResponseUnverifiable:
            return _ambiguous_purchase_response(db, operacion_id)

    if reserva_data is None and resp_reserva is not None and resp_reserva.status_code != 200:
        t_err = tic_lamport()
        dialogo_consenso.append({
            "paso": 2,
            "servidor": "eventos",
            "fase": "PREPARE/VOTE",
            "accion": "Reserva de stock",
            "respuesta": f"VOTO_NEGATIVO (HTTP {resp_reserva.status_code})",
            "cumplio_secuencia": False,
            "reloj_logico_T": t_err,
        })
        failure = {
                "mensaje": "No se pudo reservar el stock solicitado",
                "decision_consenso": "GLOBAL_ABORT",
                "dialogo_consenso": dialogo_consenso,
                "operacion_id": operacion_id,
                "recuperacion_requerida": resp_reserva.status_code >= 500,
            }
        body = {"detail": failure}
        if resp_reserva.status_code >= 500:
            # A server error can happen after Eventos committed its reservation.
            # Leave the key resumable instead of caching an ambiguous outcome.
            db.rollback()
        else:
            _complete_idempotency(idempotency_record, resp_reserva.status_code, body)
            db.commit()
        return JSONResponse(status_code=resp_reserva.status_code, content=body)

    if reserva_data is None:
        try:
            reserva_data = _resolve_reservation(
                resp_reserva, operacion_id, datos.evento_id, datos.cantidad,
            )
        except EventsResponseUnverifiable:
            return _ambiguous_purchase_response(db, operacion_id)

    _test_crash_at(test_crash_at, "after_reservation", operacion_id, test_hold_at)
    t_fin_e = tic_lamport(reserva_data.get("reloj_logico_T", t_msg_eventos))
    dialogo_consenso.append({
        "paso": 2,
        "servidor": "eventos",
        "fase": "PREPARE/VOTE",
        "accion": f"Reserva de stock (Evento #{datos.evento_id})",
        "respuesta": f"VOTO_AFIRMATIVO (Stock bloqueado con FOR UPDATE. Quedan {reserva_data.get('stock_restante')})",
        "cumplio_secuencia": True,
        "reloj_logico_T": t_fin_e,
        "exclusion_mutua": reserva_data.get("exclusion_mutua"),
    })

    # Paso 3: Persistencia Local en Compras (con inyección de imprevistos)
    compra_persistida = False
    try:
        # Simulación de imprevisto como apagar la máquina antes o durante el commit
        if tipo_imprevisto == "apagon_maquina":
            raise RuntimeError(
                "⚡ IMPREVISTO CRÍTICO: La máquina se apagó abruptamente por corte de energía antes de confirmar la transacción."
            )
        elif tipo_imprevisto == "fallo_db" or datos.simular_fallo:
            raise RuntimeError(
                "💥 FALLO DE ALMACENAMIENTO: Error de persistencia en disco de compras_db antes de commit."
            )

        compra = Compra(
            usuario_id=principal.user_id,
            evento_id=datos.evento_id,
            cantidad=datos.cantidad,
            estado="confirmada",
            instancia=INSTANCE_ID,
            operacion_id=operacion_id,
            auth_subject=principal.subject,
        )
        db.add(compra)
        db.flush()
        t_fin_c = tic_lamport()
        dialogo_consenso.append({
            "paso": 3,
            "servidor": f"compras (instancia {INSTANCE_ID})",
            "fase": "COMMIT/GLOBAL",
            "accion": "Persistencia de la transacción en compras_db",
            "respuesta": f"VOTO_AFIRMATIVO (Transacción comprometida en BD con ID #{compra.id})",
            "cumplio_secuencia": True,
            "reloj_logico_T": t_fin_c,
        })
        resultado_idempotente = {
            "id": compra.id,
            "operacion_id": operacion_id,
            "confirmacion_reserva": "pendiente",
            "usuario_id": compra.usuario_id,
            "evento_id": compra.evento_id,
            "cantidad": compra.cantidad,
            "estado": compra.estado,
            "atendido_por": f"compras (instancia {INSTANCE_ID})",
            "es_lider": ESTADO_LIDER["es_lider"],
            "stock_restante": reserva_data.get("stock_restante"),
            "decision_consenso": "GLOBAL_COMMIT",
            "sincronizacion_permitio_transaccion": True,
            "reloj_logico_T": tic_lamport(),
            "dialogo_consenso": dialogo_consenso,
            "exclusion_mutua": reserva_data.get("exclusion_mutua"),
        }
        _complete_idempotency(idempotency_record, 200, resultado_idempotente)
        db.commit()
        compra_persistida = True
        # Ventana: compra y respuesta guardadas, reserva aún sin confirmar en Eventos.
        _test_crash_at(test_crash_at, "after_purchase_commit", operacion_id, test_hold_at)
        db.refresh(compra)

        registrar_journal(
            "COMMIT",
            f"Compra #{compra.id} confirmada exitosamente ({datos.cantidad} entradas, Evento #{datos.evento_id})",
            {"compra_id": compra.id, "usuario_id": principal.user_id, "auth_subject": principal.subject, "stock_restante": reserva_data.get("stock_restante")},
            t_fin_c,
        )

    except (SQLAlchemyError, RuntimeError) as e:
        db.rollback()
        # The business transaction rolled back, but the durable idempotency
        # claim survives it. Reacquire its lock before recovery/compensation so
        # another replica cannot execute this key concurrently.
        idempotency_record = db.query(IdempotencyRequest).filter(
            IdempotencyRequest.idempotency_key == idempotency_key
        ).with_for_update().first()
        if idempotency_record is None:
            raise HTTPException(
                status_code=503,
                detail={"mensaje": "La reclamación idempotente no está disponible; no se liberó la reserva automáticamente.",
                        "operacion_id": operacion_id, "recuperacion_requerida": True},
            ) from e
        # Un error posterior al COMMIT no debe compensar una compra que ya quedó
        # persistida. Se consulta por operacion_id para resolver el resultado local.
        compra_guardada = None
        verificacion_commit_fallida = False
        if not compra_persistida:
            try:
                compra_guardada = db.query(Compra).filter(Compra.operacion_id == operacion_id).first()
            except SQLAlchemyError:
                # Un resultado de COMMIT ambiguo nunca debe compensarse a ciegas:
                # podría existir ya una compra confirmada en PostgreSQL.
                verificacion_commit_fallida = True
        if verificacion_commit_fallida:
            raise HTTPException(
                status_code=503,
                detail={
                    "mensaje": "No se pudo verificar el resultado del commit; no se liberó la reserva automáticamente.",
                    "operacion_id": operacion_id,
                    "recuperacion_requerida": True,
                },
            )
        if compra_guardada is not None or compra_persistida:
            compra = compra_guardada or compra
            registrar_journal(
                "COMMIT_RECUPERADO",
                f"Compra asociada a {operacion_id} ya estaba persistida pese a un error posterior al commit.",
                {"compra_id": getattr(compra, "id", None), "operacion_id": operacion_id, "error": str(e)},
            )
        else:
            t_fallo_c = tic_lamport()
            dialogo_consenso.append({
                "paso": 3,
                "servidor": f"compras (instancia {INSTANCE_ID})",
                "fase": "PREPARE/VOTE",
                "accion": "Persistencia en base de datos compras_db",
                "respuesta": f"VOTO_NEGATIVO ({str(e)})",
                "cumplio_secuencia": False,
                "reloj_logico_T": t_fallo_c,
            })

            # --- TRANSACCIÓN COMPENSATORIA (SAGA ROLLBACK) ---
            t_msg_lib = tic_lamport()
            compensacion_exitosa = False
            stock_restante_compensado = None
            try:
                release_data = _perform_event_action(
                    "liberar", operacion_id, datos.evento_id, datos.cantidad, t_msg_lib,
                )
                compensacion_exitosa = True
                stock_restante_compensado = release_data.get("stock_restante")
            except EventsResponseUnverifiable:
                compensacion_exitosa = False

            t_fin_comp = tic_lamport()
            dialogo_consenso.append({
                "paso": 4,
                "servidor": "eventos",
                "fase": "COMPENSACION_SAGA",
                "accion": "Rollback distribuido por imprevisto",
                "respuesta": (
                    f"VOTO_COMPENSADO (Stock restaurado a {stock_restante_compensado} entradas)"
                    if compensacion_exitosa
                    else "ERROR_COMPENSACION (No se pudo contactar a Eventos)"
                ),
                "cumplio_secuencia": compensacion_exitosa,
                "reloj_logico_T": t_fin_comp,
            })

            registrar_journal(
                "ROLLBACK",
                f"Transacción abortada por imprevisto: {str(e)}. Compensación ejecutada en Evento #{datos.evento_id}.",
                {"stock_restaurado": stock_restante_compensado, "imprevisto": tipo_imprevisto},
                t_fin_comp,
            )

            failure_body = {"detail": {
                    "mensaje": f"Transacción abortada por imprevisto: {str(e)}",
                    "imprevisto": tipo_imprevisto,
                    "decision_consenso": "GLOBAL_ABORT",
                    "sincronizacion_impidio_transaccion": True,
                    "recuperacion_automatica": True,
                    "stock_restaurado": stock_restante_compensado,
                    "datos_previos_preservados": True,
                    "dialogo_consenso": dialogo_consenso,
                    "reloj_logico_T": t_fin_comp,
                }}
            _complete_idempotency(idempotency_record, 500, failure_body)
            try:
                db.add(idempotency_record)
                db.commit()
            except SQLAlchemyError:
                db.rollback()
            return JSONResponse(status_code=500, content=failure_body)

    # La compra ya está persistida. Confirmar la reserva deja una traza correlacionada.
    # Si Eventos no responde, la compra no se duplica ni se compensa a ciegas: queda
    # marcada como pendiente y /compras/recuperar-servidor podrá reconciliarla.
    confirmacion_reserva = "pendiente"
    try:
        resp_confirmar = requests.post(
            f"{EVENTOS_URL}/eventos/{datos.evento_id}/confirmar",
            params={"operacion_id": operacion_id, "reloj_t": tic_lamport()},
            headers=eventos_headers(),
            timeout=5,
        )
        if resp_confirmar.status_code == 200:
            try:
                _validate_action_payload(
                    _events_json_object(resp_confirmar, "confirmación"),
                    "confirmar", operacion_id, datos.evento_id, datos.cantidad,
                )
                confirmacion_reserva = "confirmada"
            except EventsResponseUnverifiable:
                pass
    except requests.exceptions.RequestException:
        pass
    if confirmacion_reserva != "confirmada":
        try:
            durable = _read_durable_reservation(operacion_id, datos.evento_id, datos.cantidad)
            if durable is not None and durable["estado"] == "confirmada":
                confirmacion_reserva = "confirmada"
        except EventsResponseUnverifiable:
            pass
    if confirmacion_reserva != "confirmada":
        registrar_journal(
            "CONFIRMACION_PENDIENTE",
            f"Compra #{compra.id} persistida; confirmación de reserva pendiente en Eventos.",
            {"compra_id": compra.id, "operacion_id": operacion_id},
        )

    t_final = tic_lamport()
    resultado_idempotente["confirmacion_reserva"] = confirmacion_reserva
    resultado_idempotente["reloj_logico_T"] = t_final
    try:
        stored = db.query(IdempotencyRequest).filter(
            IdempotencyRequest.idempotency_key == idempotency_key
        ).with_for_update().first()
        if stored is not None:
            stored.response_body = resultado_idempotente
            db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        # La compra ya existe, pero no afirmar respuesta final durable hasta que
        # PostgreSQL guarde el mismo estado observado en Eventos. La recuperación
        # puede completar esta actualización con el mismo operacion_id.
        if confirmacion_reserva == "confirmada":
            raise HTTPException(
                status_code=503,
                detail={
                    "mensaje": "Compra persistida y reserva confirmada, pero no se pudo actualizar el resultado idempotente; reintente con la misma clave.",
                    "operacion_id": operacion_id,
                    "recuperacion_requerida": True,
                },
            ) from exc
    _test_crash_at(test_crash_at, "after_final_response_commit", operacion_id, test_hold_at)
    return resultado_idempotente


# =========================================================================
# TRANSACCIÓN GRUPAL ATÓMICA (BATCH TRANSACTION)
# =========================================================================
@app.post("/compras/grupal")
def crear_compra_grupal(datos: TransaccionGrupalRequest, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Ejecuta una TRANSACCIÓN GRUPAL atómica (All-or-Nothing):
    1. Valida al comprador común o grupal.
    2. Realiza la reserva secuencial con exclusión mutua de cada ítem del grupo.
    3. Si CUALQUIER ítem falla o surge un imprevisto (ej. stock agotado o apagón forzado),
       la transacción se aborta por completo (GLOBAL_ABORT) y se ejecuta la
       compensación en cascada de TODOS los ítems reservados previamente.
    4. Si todos los ítems triunfan, se persisten en bloque en compras_db (db.commit()).
    """
    validar_nodo_disponible()
    require_admin_for_simulation(datos, principal)

    if not datos.items:
        raise HTTPException(status_code=400, detail="El lote de compra grupal no contiene ningún ítem.")

    t_inicio = tic_lamport(datos.reloj_t)
    dialogo_consenso = []
    items_reservados_con_exito = []
    operaciones_ambiguas: list[dict] = []

    dialogo_consenso.append({
        "paso": 1,
        "servidor": "compras",
        "accion": "Validar subject y rol del token firmado por Usuarios",
        "respuesta": "VOTO_AFIRMATIVO (identidad autenticada)",
        "reloj_t": tic_lamport(),
    })

    # Paso 2: Reservar cada ítem del lote
    lote_persistido = False
    try:
        for idx, item in enumerate(datos.items):
            # Verificar si se inyecta un imprevisto en el último ítem
            if datos.tipo_imprevisto == "corte_red" and idx == len(datos.items) - 1:
                raise RuntimeError("🌐 CORTE DE RED EN TRÁNSITO: El paquete para reservar el ítem no llegó.")
            elif datos.tipo_imprevisto == "apagon_maquina" and idx == len(datos.items) - 1:
                raise RuntimeError("⚡ APAGÓN ABRUPTO: Máquina desconectada durante el procesamiento del ítem grupal.")

            t_msg_e = tic_lamport()
            operacion_id_item = uuid4().hex
            resp_res = None
            transport_failed = False
            try:
                resp_res = requests.post(
                    f"{EVENTOS_URL}/eventos/{item.evento_id}/reservar",
                    params={"cantidad": item.cantidad, "reloj_t": t_msg_e, "operacion_id": operacion_id_item},
                    headers=eventos_headers(), timeout=5,
                )
            except (requests.exceptions.RequestException, HTTPException):
                transport_failed = True

            if transport_failed or resp_res.status_code == 200 or resp_res.status_code >= 500:
                try:
                    res_data = _resolve_reservation(
                        resp_res, operacion_id_item, item.evento_id, item.cantidad,
                        transport_failed=transport_failed,
                    )
                except EventsResponseUnverifiable:
                    operaciones_ambiguas.append({
                        "operacion_id": operacion_id_item, "evento_id": item.evento_id,
                        "cantidad": item.cantidad,
                    })
                    raise
            else:
                raise RuntimeError(f"Eventos rechazó el ítem #{idx + 1} (HTTP {resp_res.status_code})")

            items_reservados_con_exito.append({
                "evento_id": item.evento_id,
                "cantidad": item.cantidad,
                "operacion_id": operacion_id_item,
                "stock_restante": res_data.get("stock_restante"),
            })

            dialogo_consenso.append({
                "paso": f"2.{idx + 1}",
                "servidor": "eventos",
                "accion": f"Reserva ítem #{idx + 1} (Evento {item.evento_id}, cant: {item.cantidad})",
                "respuesta": f"VOTO_AFIRMATIVO (Stock restante: {res_data.get('stock_restante')})",
                "reloj_t": tic_lamport(res_data.get("reloj_logico_T", t_msg_e)),
            })

        # Paso 3: Fallo forzado en persistencia si se solicitó
        if datos.tipo_imprevisto == "fallo_db" or datos.simular_fallo:
            raise RuntimeError("💥 ERROR DE PERSISTENCIA: Fallo al comprometer el lote grupal en compras_db.")

        # Paso 4: Persistencia atómica de todos los boletos del grupo
        compras_creadas = []
        for idx, item in enumerate(datos.items):
            c = Compra(
                usuario_id=principal.user_id,
                evento_id=item.evento_id,
                cantidad=item.cantidad,
                estado="confirmada",
                instancia=INSTANCE_ID,
                operacion_id=items_reservados_con_exito[idx]["operacion_id"],
                auth_subject=principal.subject,
            )
            db.add(c)
            compras_creadas.append(c)

        db.commit()
        lote_persistido = True
        for c in compras_creadas:
            db.refresh(c)

        confirmaciones_stock = []
        for reserva in items_reservados_con_exito:
            try:
                _perform_event_action(
                    "confirmar", reserva["operacion_id"], reserva["evento_id"],
                    reserva["cantidad"], tic_lamport(),
                )
                confirmaciones_stock.append({
                    "operacion_id": reserva["operacion_id"],
                    "evento_id": reserva["evento_id"],
                    "confirmada": True,
                })
            except EventsResponseUnverifiable:
                confirmaciones_stock.append({
                    "operacion_id": reserva["operacion_id"],
                    "evento_id": reserva["evento_id"],
                    "confirmada": False,
                    "estado": "no_verificable",
                })

        t_fin = tic_lamport()
        dialogo_consenso.append({
            "paso": 3,
            "servidor": f"compras (instancia {INSTANCE_ID})",
            "accion": f"Commit en bloque de {len(compras_creadas)} compras en compras_db",
            "respuesta": "VOTO_AFIRMATIVO (Lote registrado con éxito)",
            "reloj_t": t_fin,
        })

        registrar_journal(
            "GRUPO_COMMIT",
            f"Transacción grupal completada: {len(compras_creadas)} ítems confirmados para Usuario #{principal.user_id}",
            {"total_items": len(compras_creadas), "compras_ids": [c.id for c in compras_creadas], "confirmaciones_stock": confirmaciones_stock},
            t_fin,
        )

        return {
            "decision_consenso": "GLOBAL_COMMIT",
            "mensaje": "Transacción grupal atómica completada exitosamente.",
            "total_items": len(compras_creadas),
            "confirmaciones_stock": confirmaciones_stock,
            "confirmacion_stock_completa": all(x.get("confirmada") for x in confirmaciones_stock),
            "atendido_por": f"compras (instancia {INSTANCE_ID})",
            "compras": [
                {
                    "id": c.id,
                    "evento_id": c.evento_id,
                    "cantidad": c.cantidad,
                    "estado": c.estado,
                }
                for c in compras_creadas
            ],
            "dialogo_consenso": dialogo_consenso,
            "reloj_logico_T": t_fin,
        }

    except (SQLAlchemyError, RuntimeError, EventsResponseUnverifiable) as e:
        db.rollback()
        if lote_persistido:
            # No liberar reservas después de que el lote ya quedó persistido: eso
            # haría que el stock y el historial diverjan. La recuperación conciliará.
            op_ids = [x["operacion_id"] for x in items_reservados_con_exito]
            guardadas = db.query(Compra).filter(Compra.operacion_id.in_(op_ids)).all() if op_ids else []
            registrar_journal(
                "GRUPO_COMMIT_RECUPERACION",
                "El lote ya estaba persistido; ocurrió un error posterior y requiere conciliación de reservas.",
                {"operacion_ids": op_ids, "error": str(e)},
            )
            return {
                "decision_consenso": "GLOBAL_COMMIT",
                "mensaje": "El lote quedó persistido; se requiere reconciliar la confirmación de reservas.",
                "recuperacion_requerida": True,
                "operaciones_ambiguas": operaciones_ambiguas,
                "error_posterior_al_commit": str(e),
                "compras": [{"id": c.id, "evento_id": c.evento_id, "cantidad": c.cantidad, "estado": c.estado, "operacion_id": c.operacion_id} for c in guardadas],
                "atendido_por": f"compras (instancia {INSTANCE_ID})",
            }

        # --- COMPENSACIÓN EN CASCADA (SAGA ROLLBACK DE TODO EL LOTE) ---
        compensaciones = []
        for res in items_reservados_con_exito:
            try:
                release_data = _perform_event_action(
                    "liberar", res["operacion_id"], res["evento_id"],
                    res["cantidad"], tic_lamport(),
                )
                compensaciones.append({
                    "evento_id": res["evento_id"],
                    "operacion_id": res["operacion_id"],
                    "cantidad_liberada": res["cantidad"],
                    "estado": "liberado_con_exito",
                    "http_status": 200,
                    "stock_restante": release_data.get("stock_restante"),
                })
            except EventsResponseUnverifiable:
                compensaciones.append({
                    "evento_id": res["evento_id"],
                    "operacion_id": res["operacion_id"],
                    "cantidad_liberada": res["cantidad"],
                    "estado": "estado_no_verificable",
                })

        t_abort = tic_lamport()
        dialogo_consenso.append({
            "paso": "ROLLBACK_GRUPAL",
            "servidor": "eventos",
            "accion": f"Compensación en cascada de {len(items_reservados_con_exito)} ítems",
            "respuesta": "COMPENSACION_COMPLETA" if all(x.get("estado") == "liberado_con_exito" for x in compensaciones) else "COMPENSACION_PARCIAL_REQUIERE_RECUPERACION",
            "reloj_t": t_abort,
        })

        registrar_journal(
            "GRUPO_ABORT",
            f"Transacción grupal ABORTADA: {str(e)}. Se compensaron {len(compensaciones)} ítems reservados.",
            {"compensaciones": compensaciones, "imprevisto": datos.tipo_imprevisto},
            t_abort,
        )

        raise HTTPException(
            status_code=503 if operaciones_ambiguas else 409,
            detail={
                "mensaje": "Transacción grupal requiere recuperación por un resultado remoto no verificable." if operaciones_ambiguas else "Transacción grupal rechazada por disenso o imprevisto.",
                "decision_consenso": "GLOBAL_ABORT",
                "atomicidad_grupal": "Saga compensatoria; revisar el resultado real de cada compensación",
                "items_compensados": compensaciones,
                "operaciones_ambiguas": operaciones_ambiguas,
                "stock_restaurado_en_todos_los_eventos": all(x.get("estado") == "liberado_con_exito" for x in compensaciones),
                "requiere_recuperacion": not all(x.get("estado") == "liberado_con_exito" for x in compensaciones),
                "dialogo_consenso": dialogo_consenso,
                "reloj_logico_T": t_abort,
            },
        )


# =========================================================================
# AUDITORÍA DE CONSISTENCIA DE DATOS EN TIEMPO REAL
# =========================================================================
@app.get("/auditoria/consistencia")
def auditar_consistencia(db: Session = Depends(get_db), _admin: Principal = Depends(require_admin)):
    """Audita el stock contra las compras confirmadas y detecta reservas pendientes.

    Eventos es la fuente de verdad para el stock inicial y actual. Si Eventos no
    responde, la auditoría falla explícitamente en vez de sustituir datos por
    valores codificados o declarar una consistencia que no se pudo comprobar.
    """
    validar_nodo_disponible()
    try:
        r_ev = requests.get(f"{EVENTOS_URL}/eventos/auditoria/stock", headers=eventos_headers(), timeout=5)
        if r_ev.status_code != 200:
            raise EventsResponseUnverifiable("Eventos no respondió la auditoría de stock")
        eventos_info = _validate_stock_audit(_events_json_payload(r_ev, "auditoría de stock"))
        r_pend = requests.get(f"{EVENTOS_URL}/eventos/reservas/pendientes", headers=eventos_headers(), timeout=5)
        if r_pend.status_code != 200:
            raise EventsResponseUnverifiable("Eventos no respondió la lista de reservas")
        reservas_pendientes = _validate_pending_reservations(_events_json_object(r_pend, "reservas pendientes"))
    except (requests.exceptions.RequestException, EventsResponseUnverifiable, HTTPException):
        raise HTTPException(
            status_code=503,
            detail={
                "mensaje": "No se puede completar la auditoría porque el servicio Eventos no respondió con datos verificables.",
                "dependencia": "eventos",
                "todo_consistente": None,
            },
        )

    compras_confirmadas = db.query(Compra).filter(Compra.estado == "confirmada").all()
    compras_por_evento = {}
    compras_por_operacion = {}
    compras_legacy_sin_operacion = 0
    for compra in compras_confirmadas:
        compras_por_evento[compra.evento_id] = compras_por_evento.get(compra.evento_id, 0) + compra.cantidad
        if compra.operacion_id:
            compras_por_operacion[compra.operacion_id] = compra
        else:
            compras_legacy_sin_operacion += 1

    # Comparar el journal funcional con compras confirmadas. Las brechas se
    # informan aparte: el journal es best-effort y no sustituye compras_db.
    commits_journal = db.query(RegistroJournal).filter(
        RegistroJournal.tipo.in_(("COMMIT", "COMMIT_RECUPERADO"))
    ).all()
    journal_purchase_ids = set()
    for entry in commits_journal:
        try:
            purchase_id = (entry.detalles or {}).get("compra_id")
            if purchase_id is not None:
                journal_purchase_ids.add(int(purchase_id))
        except (TypeError, ValueError):
            continue
    confirmed_purchase_ids = {purchase.id for purchase in compras_confirmadas}
    compras_sin_journal_commit = sorted(confirmed_purchase_ids - journal_purchase_ids)
    journal_commits_sin_compra = sorted(journal_purchase_ids - confirmed_purchase_ids)
    journal_completo = not compras_sin_journal_commit and not journal_commits_sin_compra

    ids_eventos = {int(e["id"]) for e in eventos_info if "id" in e}
    compras_huerfanas = [
        {"compra_id": c.id, "evento_id": c.evento_id, "cantidad": c.cantidad}
        for c in compras_confirmadas if c.evento_id not in ids_eventos
    ]
    reporte_eventos = []
    todo_consistente = not bool(reservas_pendientes) and not bool(compras_huerfanas)
    total_stock_inicial = total_stock_actual = total_vendidas = 0
    operaciones_eventos = {}

    for ev in eventos_info:
        ev_id = int(ev["id"])
        nombre = ev.get("nombre", f"Evento {ev_id}")
        stock_actual_raw = ev.get("stock_actual", ev.get("stock", -1))
        stock_inicial_raw = ev.get("stock_inicial", -1)
        stock_actual = int(stock_actual_raw) if stock_actual_raw is not None else -1
        stock_inicial = int(stock_inicial_raw) if stock_inicial_raw is not None else -1
        vendidas = int(compras_por_evento.get(ev_id, 0))
        stock_esperado = stock_inicial - vendidas
        diferencia = stock_actual - stock_esperado
        stock_valido = stock_actual >= 0 and stock_inicial >= 0 and vendidas >= 0
        consistente = stock_valido and diferencia == 0
        if not consistente:
            todo_consistente = False
        total_stock_inicial += max(stock_inicial, 0)
        total_stock_actual += max(stock_actual, 0)
        total_vendidas += vendidas
        ids_ops = ev.get("operaciones_confirmadas", [])
        for op in ids_ops:
            if isinstance(op, dict) and op.get("operacion_id"):
                operaciones_eventos[str(op["operacion_id"])] = {
                    "evento_id": ev_id,
                    "cantidad": int(op.get("cantidad", 0)),
                }
            elif isinstance(op, str):  # Compatibilidad con respuesta de versión anterior.
                operaciones_eventos[op] = {"evento_id": ev_id, "cantidad": None}
        reporte_eventos.append({
            "evento_id": ev_id,
            "nombre": nombre,
            "stock_inicial_origen": stock_inicial,
            "entradas_vendidas": vendidas,
            "stock_actual_disponible": stock_actual,
            "stock_esperado": stock_esperado,
            "invariante_esperada": stock_esperado,  # Compatibilidad con el frontend anterior.
            "diferencia": diferencia,
            "es_consistente": consistente,
            "estado": "CONSISTENTE" if consistente else "INCONSISTENTE",
        })

    operaciones_compras = set(compras_por_operacion.keys())
    ids_operaciones_eventos = set(operaciones_eventos.keys())
    operaciones_sin_compra = sorted(ids_operaciones_eventos - operaciones_compras)
    compras_sin_reserva_confirmada = sorted(operaciones_compras - ids_operaciones_eventos)
    operaciones_evento_incorrecto = sorted(
        op_id for op_id in operaciones_compras & ids_operaciones_eventos
        if compras_por_operacion[op_id].evento_id != operaciones_eventos[op_id]["evento_id"]
    )
    operaciones_cantidad_incorrecta = sorted(
        op_id for op_id in operaciones_compras & ids_operaciones_eventos
        if operaciones_eventos[op_id]["cantidad"] is not None
        and compras_por_operacion[op_id].cantidad != operaciones_eventos[op_id]["cantidad"]
    )
    # Las compras antiguas no tenían operacion_id; se informan, pero su consistencia
    # se evalúa mediante la ecuación de stock. Las operaciones nuevas se correlacionan
    # por identificador, evento y cantidad.
    operaciones_correlacionadas_consistentes = not (
        operaciones_sin_compra or compras_sin_reserva_confirmada
        or operaciones_evento_incorrecto or operaciones_cantidad_incorrecta
    )
    if not operaciones_correlacionadas_consistentes:
        todo_consistente = False

    balance_global = total_stock_inicial - (total_stock_actual + total_vendidas)
    if balance_global != 0:
        todo_consistente = False
    t = tic_lamport()
    return {
        "todo_consistente": todo_consistente,
        "auditoria_completa": compras_legacy_sin_operacion == 0,
        "balance_global_diferencia": balance_global,
        "ecuacion": "Stock_Inicial == Stock_Actual + Entradas_Vendidas",
        "veredicto": (
            "CONSISTENTE: stock y compras coinciden, sin reservas pendientes ni operaciones nuevas huérfanas."
            if todo_consistente and compras_legacy_sin_operacion == 0
            else "CONSISTENTE SEGÚN ECUACIÓN DE STOCK, PERO HAY COMPRAS HISTÓRICAS SIN CORRELACIÓN INDIVIDUAL."
            if todo_consistente
            else "INCONSISTENCIA O TRANSACCIÓN PENDIENTE DETECTADA; revisar los detalles del reporte."
        ),
        "totales": {
            "stock_inicial": total_stock_inicial,
            "entradas_vendidas": total_vendidas,
            "stock_actual_disponible": total_stock_actual,
            "total_transacciones_compras": len(compras_confirmadas),
            "compras_legacy_sin_operacion_id": compras_legacy_sin_operacion,
            "reservas_pendientes": len(reservas_pendientes),
            "registros_journal_commit": len(commits_journal),
        },
        "reservas_pendientes": reservas_pendientes,
        "compras_huerfanas": compras_huerfanas,
        "operaciones_confirmadas_sin_compra": operaciones_sin_compra,
        "compras_sin_reserva_confirmada": compras_sin_reserva_confirmada,
        "journal": {
            "completo": journal_completo,
            "compras_confirmadas_sin_commit_journal": compras_sin_journal_commit,
            "commits_journal_sin_compra_confirmada": journal_commits_sin_compra,
            "es_fuente_de_verdad": False,
        },
        "operaciones_con_evento_incorrecto": operaciones_evento_incorrecto,
        "operaciones_con_cantidad_incorrecta": operaciones_cantidad_incorrecta,
        "reporte_por_evento": reporte_eventos,
        "reloj_logico_T": t,
    }


# =========================================================================
# BITÁCORA DE EVENTOS EN MEMORIA
# =========================================================================


@app.post("/compras/journal/limpiar")
def limpiar_journal(db: Session = Depends(get_db), _admin: Principal = Depends(require_admin)):
    """Limpia la bitácora funcional persistente (solo para demostraciones)."""
    global JOURNAL_TRANSACCIONES
    eliminadas = db.query(RegistroJournal).delete(synchronize_session=False)
    db.commit()
    JOURNAL_TRANSACCIONES.clear()
    return {"status": "ok", "entradas_eliminadas": eliminadas, "mensaje": "Bitácora persistente reinicializada."}


from shared import telemetry
telemetry.install(app, "compras")

import models as checkout_models
from shared import checkout
order_coordinator = checkout.install(app, checkout_models, SessionLocal, get_db, get_principal,
                                    require_admin, EVENTOS_URL, INSTANCE_ID)

@app.on_event("startup")
def start_orders():
    if engine.dialect.name == "postgresql":
        order_coordinator.start()

@app.on_event("shutdown")
def stop_orders():
    order_coordinator.stop.set()

from shared import diagnostics
diagnostics.install(app, get_db, require_admin)

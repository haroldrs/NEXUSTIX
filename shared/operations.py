"""Observación administrativa acotada. No ejecuta acciones de infraestructura.

Los GET no escriben: solo las capturas/importaciones explícitas crean evidencia.
Las URL proceden exclusivamente de configuración del servidor, nunca del cliente.
"""
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import requests
from fastapi import Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, text
from sqlalchemy.exc import SQLAlchemyError

from shared.auth_tokens import service_auth_headers
from shared import node_operations

STATES = Literal["saludable", "degradado", "caido", "desconocido"]
SCENARIO = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P"]
RECOVERY_STAGE = Literal["falla", "aislamiento", "replica", "promocion", "conexiones", "servicios", "integridad"]
KNOWN = {
    "frontend": ("Frontend", "frontend", []),
    "gateway": ("Balanceador / gateway", "gateway", ["compras1", "compras2"]),
    "usuarios_entrada": ("Ruta de Usuarios", "ruta", ["usuarios_a", "usuarios_b"]),
    "eventos_entrada": ("Ruta de Eventos", "ruta", ["eventos_a", "eventos_b"]),
    "usuarios_a": ("Usuarios A", "api", ["usuarios_db"]),
    "usuarios_b": ("Usuarios B", "api", ["usuarios_db"]),
    "eventos_a": ("Eventos A", "api", ["eventos_db"]),
    "eventos_b": ("Eventos B", "api", ["eventos_db"]),
    "compras1": ("Compras 1", "api", ["compras_db", "eventos_entrada"]),
    "compras2": ("Compras 2", "api", ["compras_db", "eventos_entrada"]),
}
DATABASES = {"usuarios_db", "eventos_db", "compras_db"}


def now():
    return datetime.now(timezone.utc)


def clean(value, limit=1500):
    """Redacta credenciales reconocibles; nunca recibe cuerpos de negocio completos."""
    value = str(value or "")[:limit]
    value = re.sub(r"-----BEGIN [^-]+-----[\s\S]*", "[clave omitida]", value)
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [omitido]", value)
    value = re.sub(r"(?i)(?:postgres(?:ql)?|https?)://[^\s]+", "[URL omitida]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[token omitido]", value)
    value = re.sub(r"(?i)(password|contrase(?:ña|na)|secret|token|authorization|cookie|qr|dsn)\s*[:=]\s*[^\s,;]+", r"\1=[omitido]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[correo omitido]", value)
    value = re.sub(r"\b[A-Za-z0-9_+/=-]{40,}\b", "[valor sensible omitido]", value)
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32)


class OperatorMeasures(BaseModel):
    # Datos aportados por quien ejecuta el experimento: nunca se presentan como medidos por el sistema.
    model_config = {"extra": "forbid"}
    inicio_falla: datetime | None = None
    restauracion: datetime | None = None
    operacion: str = Field(default="", max_length=100)
    intentadas: int | None = Field(default=None, ge=0, le=10000000)
    correctas: int | None = Field(default=None, ge=0, le=10000000)
    fallidas: int | None = Field(default=None, ge=0, le=10000000)
    rto_objetivo_segundos: float | None = Field(default=None, ge=0, le=31536000, allow_inf_nan=False)
    rpo_objetivo: str = Field(default="", max_length=120)
    ultima_transaccion_reconocida: str = Field(default="", max_length=120)
    ultima_transaccion_recuperada: str = Field(default="", max_length=120)
    perdida_observada: str = Field(default="", max_length=300)


class CaptureRequest(BaseModel):
    model_config = {"extra": "forbid"}
    escenario: SCENARIO
    fase: Literal["inicial", "final", "observacion"] = "observacion"
    referencia_inicial: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    referencia_falla: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    nodo_objetivo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    pasos_recuperacion: list[RECOVERY_STAGE] = Field(default_factory=list, max_length=7)
    mediciones: OperatorMeasures | None = None
    acciones: str = Field(default="", max_length=1500)
    observado: str = Field(default="", max_length=2000)
    conclusion: str = Field(default="", max_length=1500)


class ImportedComponent(BaseModel):
    # La importación conserva solo esta lista blanca, nunca payloads arbitrarios.
    model_config = {"extra": "ignore"}
    id: str = Field(pattern=r"^[a-z0-9_]{1,48}$")
    estado: STATES
    comprobado_en: datetime
    latencia_ms: float | None = Field(default=None, ge=0, le=3600000, allow_inf_nan=False)
    instancia: str | None = Field(default=None, max_length=80)
    nodo: str | None = Field(default=None, max_length=80)


class ImportedSnapshot(BaseModel):
    model_config = {"extra": "ignore"}
    capturado_en: datetime
    componentes: list[ImportedComponent] = Field(max_length=512)


class ImportRequest(BaseModel):
    model_config = {"extra": "ignore"}
    escenario: SCENARIO
    fecha: datetime
    acciones: str = Field(default="", max_length=1500)
    observado: str = Field(default="", max_length=2000)
    conclusion: str = Field(default="", max_length=1500)
    evidencias: ImportedSnapshot


def scenarios():
    rows = [
        ("A", "Concurrencia e inventario", "Observar consistencia cuando varias solicitudes compiten por las últimas entradas.",
         ["concurrencia", "bloqueo de filas", "consistencia"], ["compras1", "compras2", "eventos_a", "eventos_db"],
         "Entorno aislado, evento autorizado y stock conocido; solicitudes identificables.",
         "El operador envía las solicitudes autorizadas y captura antes/después; el panel no genera compras.",
         "No hay sobreventa; inventario inicial = disponible + reservado + vendido.",
         "Un healthcheck no demuestra ausencia de sobreventa; contrastar inventario y operaciones persistidas."),
        ("B", "Caída de Compras 1", "Observar si Compras 2 continúa atendiendo a través del balanceador.",
         ["balanceo", "redundancia", "tolerancia a fallos"], ["gateway", "compras1", "compras2"],
         "Dos réplicas consultables y balanceador configurado hacia ambas.",
         "Operador interrumpe manualmente Compras 1 en su entorno aislado y observa el tráfico autorizado.",
         "Compras 2 atiende solicitudes; la réplica interrumpida deja de responder.",
         "Un HTTP saludable en Compras 2 no prueba por sí solo que el balanceador dirigió tráfico a ella."),
        ("C", "Caída de Eventos A", "Observar continuidad del catálogo mediante Eventos B cuando esté desplegado.",
         ["redundancia", "disponibilidad"], ["eventos_entrada", "eventos_a", "eventos_b", "eventos_db"],
         "Eventos B real y URL balanceada configurada en los clientes.",
         "Operador interrumpe manualmente Eventos A y consulta el catálogo autorizado por el gateway.",
         "Eventos B sigue sirviendo el catálogo y el gateway lo selecciona.",
         "El Compose local no incorpora Eventos B; sin esa réplica este escenario no es demostrable."),
        ("D", "Caída de Usuarios A", "Observar continuidad del servicio de usuarios mediante Usuarios B.",
         ["redundancia", "autenticación distribuida"], ["usuarios_entrada", "usuarios_a", "usuarios_b", "usuarios_db"],
         "Usuarios B desplegado, claves compatibles y gateway configurado.",
         "Operador interrumpe manualmente Usuarios A y realiza una operación autorizada por el gateway.",
         "Usuarios B atiende y valida identidades con su persistencia disponible.",
         "Tokens ya emitidos no demuestran disponibilidad de login; Usuarios B no existe en Compose local."),
        ("E", "Cancelación de evento vendido", "Seguir bloqueo de ventas y compensaciones persistentes.",
         ["Saga", "idempotencia", "transacciones locales", "compensación"], ["eventos_a", "compras1", "compras_db"],
         "Evento con ventas autorizado para cancelación; conservar IDs y estado inicial.",
         "Administrador cancela desde Gestión de eventos y captura el progreso real.",
         "Ventas bloqueadas, reembolsos simulados sin duplicados y entradas invalidadas según su estado.",
         "La compensación es eventual; un error recuperable no equivale a rollback global."),
        ("F", "Fallo durante una operación", "Seguir una misma operación antes y después de un reintento seguro.",
         ["idempotencia", "Saga", "compensación"], ["compras1", "eventos_a", "compras_db"],
         "Operación autorizada identificable y entorno aislado; conservar su clave de idempotencia de forma privada.",
         "Operador provoca el fallo autorizado fuera del navegador y reintenta la misma operación.",
         "La persistencia permite recuperar o compensar sin duplicar compras ni pagos simulados.",
         "La evidencia exportada no contiene claves de idempotencia, JWT ni QR."),
        ("G", "Reinicio con trabajos pendientes", "Observar recuperación de Sagas persistentes después de reinicio manual.",
         ["durabilidad", "recuperación", "outbox"], ["compras1", "compras2", "compras_db"],
         "Trabajo pendiente real y trabajador de recuperación habilitado en el despliegue.",
         "Operador reinicia manualmente el servicio y captura el mismo trabajo hasta su resolución.",
         "Los trabajos se reanudan desde el estado persistido; outbox conserva sus eventos.",
         "Contadores HTTP se reinician con el proceso; no sustituyen evidencia persistida."),
        ("H", "Fallo de PostgreSQL", "Distinguir indisponibilidad y recuperación de la base de datos.",
         ["persistencia", "replicación", "fencing"], ["usuarios_db", "eventos_db", "compras_db"],
         "Entorno aislado y respaldo; standby y procedimiento de fencing solo si realmente existen.",
         "Operador ejecuta el procedimiento manual autorizado; el panel solo captura diagnóstico.",
         "Las APIs reflejan el fallo; tras recuperación se verifica rol, WAL y operaciones persistidas.",
         "Sin standby no hay failover. Nunca promover desde este panel; LAN concentra PostgreSQL en PC2."),
        ("I", "Caída de una PC completa", "Identificar servicios supervivientes y puntos únicos de fallo por nodo.",
         ["fallos parciales", "distribución", "disponibilidad"], list(KNOWN),
         "Asignaciones reales de nodos configuradas, máquinas independientes y autorización manual.",
         "Operador interrumpe el nodo autorizado y captura desde un observador superviviente.",
         "Solo continúan los componentes que conservan sus dependencias y acceso de clientes.",
         "Timeout HTTP no prueba una PC apagada. En LAN PC2 concentra las tres bases sin replicación entre PC."),
        ("J", "Checkpoints y WAL", "Observar checkpoints, posiciones WAL y recuperación real de PostgreSQL.",
         ["WAL", "checkpoints", "durabilidad", "crash recovery"], ["usuarios_db", "eventos_db", "compras_db"],
         "Permisos de lectura de estadísticas y versión PostgreSQL compatible; evidencia de operaciones previas.",
         "Capturar estadísticas antes/después de actividad autorizada y recuperación manual.",
         "LSN y estadísticas describen actividad persistente; operaciones anteriores siguen disponibles.",
         "La mera presencia de WAL/checkpoints no prueba recuperación; hace falta contrastar datos anteriores/posteriores."),
        ("K", "Timeout entre Compras y Eventos", "Distinguir un resultado remoto desconocido de un rechazo.",
         ["timeouts", "fallos parciales", "comunicación síncrona"], ["compras1", "compras2", "eventos_entrada"],
         "Entorno aislado y una operación autorizada identificable.",
         "Operador retrasa o interrumpe manualmente la ruta hacia Eventos y conserva el identificador de la operación.",
         "El resultado queda pendiente de verificación; no se compensa a ciegas ni se duplica.",
         "Un timeout no indica si Eventos llegó a aplicar la reserva."),
        ("L", "Interrupción de conectividad entre nodos", "Observar el comportamiento cuando dos computadoras dejan de comunicarse.",
         ["particiones de red", "fallos parciales", "split-brain"], ["gateway", "usuarios_db", "eventos_db", "compras_db"],
         "Dos o más máquinas reales, autorización manual y un observador en cada lado.",
         "Operador corta manualmente la red entre los nodos en un entorno aislado y captura desde cada lado.",
         "Cada lado solo conserva lo que tiene accesible; ninguna base se promociona sola.",
         "La falta de conectividad no demuestra apagado. Las capturas de cada lado no forman un estado global."),
        ("M", "Interrupción del streaming WAL", "Observar el canal WAL y un standby sin capacidad de escritura.",
         ["replicación", "WAL", "consistencia"], ["usuarios_db", "eventos_db", "compras_db"],
         "Standby configurado y canal WAL observable.",
         "Operador interrumpe manualmente el canal hacia un standby y observa su diagnóstico.",
         "La primaria sigue escribiendo si es asíncrona; el standby se retrasa y sigue sin aceptar escrituras.",
         "La diferencia WAL no equivale a tiempo. En modo síncrono las escrituras pueden quedar en espera."),
        ("N", "Reincorporación controlada de una base", "Documentar cómo vuelve un nodo antiguo como standby.",
         ["recuperación", "split-brain", "timeline"], ["usuarios_db", "eventos_db", "compras_db"],
         "Promoción manual previa documentada y antiguo primario aislado.",
         "Operador sigue despliegue-nodos/RECUPERACION.md para reconstruirlo con una generación nueva.",
         "El nodo vuelve como standby y no compite como escritor.",
         "Encender un antiguo primario no lo reincorpora; su historia WAL puede ser divergente."),
        ("O", "Pérdida del gateway del navegador", "Observar qué ocurre al perder la URL usada, aunque otros servicios sigan activos.",
         ["disponibilidad", "transparencia", "punto único de fallo"], ["frontend", "gateway"],
         "Otro gateway configurado, si existe, y su dirección conocida de antemano.",
         "Operador interrumpe manualmente el gateway en uso y entra por la dirección alternativa.",
         "El acceso alternativo funciona si está configurado y es alcanzable.",
         "No hay redirección automática sin un acceso estable. Una página servida desde ese origen no recibe datos nuevos."),
        ("P", "Reserva pendiente de confirmación", "Seguir un pedido detenido entre la reserva y su confirmación.",
         ["Saga", "recuperación hacia adelante", "compensación"], ["compras1", "eventos_a", "compras_db", "eventos_db"],
         "Pedido autorizado identificable y trabajo de recuperación habilitado.",
         "Operador interrumpe manualmente un paso en un entorno aislado y sigue el pedido en Observabilidad.",
         "El pedido continúa o se compensa desde su estado durable, sin duplicar efectos.",
         "Recuperar la infraestructura no significa que los pedidos ya estén consistentes."),
    ]
    meta = {
        "A": ("comerciales", ["local", "distribuido", "multinodo"], "bajo", ["Eventos", "PostgreSQL Eventos"], "Inventario consistente tras las solicitudes.", ["Latencia", "Solicitudes correctas y fallidas"]),
        "B": ("procesos", ["local", "distribuido", "multinodo"], "medio", ["API Compras"], "La réplica vuelve y supera sus comprobaciones.", ["Disponibilidad funcional", "Ventana de observación"]),
        "C": ("procesos", ["distribuido", "multinodo"], "medio", ["API Eventos"], "La réplica vuelve y supera sus comprobaciones.", ["Disponibilidad funcional", "Ventana de observación"]),
        "D": ("procesos", ["distribuido", "multinodo"], "medio", ["API Usuarios"], "La réplica vuelve y supera sus comprobaciones.", ["Disponibilidad funcional", "Ventana de observación"]),
        "E": ("comerciales", ["local", "distribuido", "multinodo"], "medio", ["Compras", "Eventos"], "Compensaciones cerradas sin duplicados.", ["Estado de reembolsos", "Inventario"]),
        "F": ("comerciales", ["local", "distribuido", "multinodo"], "medio", ["Compras", "Eventos"], "Operación resuelta con su mismo identificador.", ["Idempotencia", "Estado final"]),
        "G": ("procesos", ["local", "distribuido", "multinodo"], "medio", ["API Compras", "Outbox"], "Trabajos pendientes reanudados y cerrados.", ["Pedidos pendientes", "Ventana de observación"]),
        "H": ("bases_datos", ["local", "distribuido", "multinodo"], "alto", ["PostgreSQL", "APIs dependientes"], "Rol, WAL y operaciones persistidas verificados.", ["RTO", "RPO", "Replicación"]),
        "I": ("fisicas", ["distribuido", "multinodo"], "alto", ["Todos los componentes del nodo"], "Servicios, escrituras y pedidos verificados tras la reincorporación.", ["Detección", "RTO", "RPO", "Disponibilidad funcional"]),
        "J": ("bases_datos", ["local", "distribuido", "multinodo"], "medio", ["PostgreSQL"], "Datos anteriores disponibles tras la recuperación.", ["Replicación", "Checkpoints"]),
        "K": ("comunicacion", ["local", "distribuido", "multinodo"], "medio", ["Compras", "Eventos"], "La operación se verifica por su identificador.", ["Latencia", "Respuestas por timeout"]),
        "L": ("comunicacion", ["multinodo"], "alto", ["Rutas entre nodos", "Replicación WAL"], "Conectividad restaurada y roles verificados.", ["Detección", "Replicación", "Disponibilidad funcional"]),
        "M": ("bases_datos", ["distribuido", "multinodo"], "medio", ["Replicación WAL"], "Canal en streaming y retraso medido.", ["Replicación"]),
        "N": ("bases_datos", ["distribuido", "multinodo"], "alto", ["PostgreSQL"], "Standby con identidad, timeline y WAL confirmados.", ["Replicación", "RTO"]),
        "O": ("fisicas", ["distribuido", "multinodo"], "medio", ["Acceso del navegador"], "Acceso restaurado o alternativa verificada.", ["Disponibilidad funcional"]),
        "P": ("comerciales", ["local", "distribuido", "multinodo"], "medio", ["Compras", "Eventos"], "Pedido en estado final verificado.", ["Estado final", "Recuperación"]),
    }
    result = [{"id": r[0], "nombre": r[1], "objetivo": r[2], "conceptos": r[3], "componentes": r[4],
             "requisitos": r[5], "accion": r[6], "esperado": r[7], "limitaciones": [r[8]],
             "categoria": meta[r[0]][0], "perfiles": meta[r[0]][1], "riesgo": meta[r[0]][2], "dependencias": meta[r[0]][3],
             "recuperacion": meta[r[0]][4], "metricas": meta[r[0]][5]} for r in rows]
    if node_operations.enabled():
        document, warnings = node_operations.load_inventory()
        if document:
            return node_operations.adapt_scenarios(result, document)
        for row in result:
            row.update({"nombre": "Escenario " + row["id"] + " · inventario no verificable", "componentes": [],
                "requisitos": "Corregir el archivo de inventario configurado en el servidor.",
                "limitaciones": warnings})
    return result


def topology(events_url):
    if node_operations.enabled():
        document, warnings = node_operations.load_inventory()
        return node_operations.component_entries(document) if document else [], warnings
    defaults = {"frontend": "http://frontend", "gateway": "http://compras_lb",
                "usuarios_entrada": os.getenv("USUARIOS_URL", "http://usuarios:8001"), "eventos_entrada": events_url,
                "usuarios_a": os.getenv("USUARIOS_URL", "http://usuarios:8001"),
                "eventos_a": events_url, "compras1": "http://compras1:8003", "compras2": "http://compras2:8003"}
    warnings = []
    configured = {}
    if os.getenv("NEXUSTIX_OPERATIONS_TOPOLOGY"):
        try:
            configured = json.loads(os.environ["NEXUSTIX_OPERATIONS_TOPOLOGY"])
            if not isinstance(configured, dict) or set(configured) - set(KNOWN) - DATABASES:
                raise ValueError()
        except (ValueError, TypeError):
            warnings.append("NEXUSTIX_OPERATIONS_TOPOLOGY inválida: destinos personalizados no consultados.")
            configured = {key: {} for key in KNOWN}
    result = []
    for key, (name, kind, deps) in KNOWN.items():
        explicit = key in configured
        entry = configured.get(key, {})
        if not isinstance(entry, dict):
            entry = {}
            explicit = True
        url = entry.get("url") if explicit else defaults.get(key)
        valid = False
        if isinstance(url, str) and len(url) <= 500:
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not (
                    parsed.username or parsed.password or parsed.query or parsed.fragment)
                _ = parsed.port
            except ValueError:
                pass
        raw_deps = entry.get("dependencias", deps)
        dependencies = [v for v in raw_deps if isinstance(v, str) and v in set(KNOWN) | DATABASES] if isinstance(raw_deps, list) else deps
        result.append({"id": key, "nombre": name, "tipo": kind,
            "nodo": clean(entry.get("nodo"), 80) or None,
            "dependencias": dependencies, "url": url.rstrip("/") if valid else None,
            "origen_configuracion": "explicita" if explicit else "convencion_local_no_verificada"})
    return result, warnings


def fetch(url, path, authorization=None, internal=False, deadline=None):
    """GET fijo, sin redirecciones, proxies ambientales ni cuerpos no acotados."""
    if not url:
        return {"ok": False, "status": None, "datos": {}, "latencia_ms": None, "motivo": "Destino no configurado"}
    start = time.monotonic()
    request_deadline = min(start + 4, deadline) if deadline is not None else start + 4
    try:
        headers = {"Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        if internal:
            headers.update(service_auth_headers())
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url + path, headers=headers, timeout=(1, 1), allow_redirects=False, stream=True) as response:
                body = bytearray()
                # Acotar también servidores que entregan lentamente el cuerpo.
                for chunk in response.iter_content(1):
                    body.extend(chunk)
                    if len(body) > 131072 or time.monotonic() >= request_deadline:
                        raise ValueError()
                try:
                    payload = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError, RecursionError):
                    payload = {}
                return {"ok": response.status_code == 200, "status": response.status_code,
                    "datos": payload if isinstance(payload, dict) else {},
                    "instancia": clean(response.headers.get("X-Instance-ID"), 80) or None,
                    "backend": clean(response.headers.get("X-Nexustix-Backend") or response.headers.get("X-Nexustix-Upstream"), 160) or None,
                    "latencia_ms": round((time.monotonic() - start) * 1000, 2),
                    "motivo": "Respuesta HTTP observada"}
    except (requests.RequestException, ValueError, RuntimeError):
        return {"ok": False, "status": None, "datos": {},
            "latencia_ms": round((time.monotonic() - start) * 1000, 2),
            "motivo": "Consulta no verificable; puede ser red, proceso, configuración o dependencia"}


def probe(entry, authorization, deadline=None):
    def read(path, credentials=None):
        if deadline is not None and time.monotonic() >= deadline:
            return {"ok": False, "status": None, "datos": {}, "latencia_ms": None,
                "motivo": "Sonda omitida por presupuesto temporal de la captura; estado no verificado"}
        return fetch(url, path, credentials, deadline=deadline)

    url = entry["url"]
    component = {key: value for key, value in entry.items() if key != "url"}
    health = read("/lb-health" if entry["tipo"] == "gateway" else "/health")
    data = health["datos"]
    status = "desconocido"
    if health["ok"]:
        status = "saludable" if data.get("status") == "ok" else "desconocido"
    elif health["status"] and health["status"] >= 500:
        status = "degradado"  # HTTP prueba respuesta, no caída del proceso/PC.
    db_state = "disponible" if data.get("db") == "connected" else "no_disponible" if data.get("db") == "unavailable" else "desconocido"
    component.update({"estado": status, "comprobado_en": now().isoformat(),
        "sonda_realizada": health["latencia_ms"] is not None,
        "latencia_ms": health["latencia_ms"], "instancia": clean(data.get("instancia"), 80) or health.get("instancia"),
        "base_datos": db_state, "http_status": health["status"], "detalle": health["motivo"],
        "alcance": "observación HTTP; no acredita el estado de contenedor o máquina"})
    if entry["tipo"] in {"gateway", "ruta"}:
        traffic = read("/health") if entry["tipo"] == "gateway" else health
        component["solicitud_observada"] = {
            "metodo": "GET", "ruta": "/health", "comprobado_en": now().isoformat(),
            "http_status": traffic["status"], "latencia_ms": traffic["latencia_ms"],
            "instancia": traffic.get("instancia") or clean(traffic["datos"].get("instancia"), 80) or None,
            "backend": traffic.get("backend"),
            "estado": "saludable" if traffic["ok"] and traffic["datos"].get("status") == "ok" else "no_verificado",
            "alcance": "Una lectura real por la ruta configurada; no prueba continuidad de escrituras ni el algoritmo de balanceo."}
        if entry["tipo"] == "gateway" and component["estado"] == "saludable" and component["solicitud_observada"]["estado"] != "saludable":
            component["estado"] = "degradado" if traffic["status"] is not None else "desconocido"
            component["detalle"] = "Gateway responde, pero su ruta hacia el frontend no tiene una lectura saludable verificable."
    diagnostic = metrics = None
    if url and entry["tipo"] == "api":
        identity = read("/diagnostico/instancia", authorization) if entry.get("origen_configuracion") == "inventario_nodos" else None
        if identity and identity["ok"]:
            component["identidad_observada"] = {key: value for key, value in identity["datos"].items()
                if key in {"instancia", "servicio", "node_id", "deployment_id", "release", "inventory_sha256", "comprobado_en"}
                and isinstance(value, str) and len(value) <= 96 and re.fullmatch(r"[A-Za-z0-9_.:+-]+", value)}
            component["respuesta_administrativa_en"] = now().isoformat()
        diagnostic = read("/diagnostico/db", authorization)
        metrics = read("/diagnostico/metricas", authorization)
    return component, diagnostic, metrics


DIAGNOSTIC_KEYS = {"disponible", "rol", "comprobado_en", "version", "en_recuperacion", "replicas", "receptor_wal", "archivado",
    "wal", "checkpoints", "ultimo_checkpoint", "conexiones", "durabilidad", "ultimo_failover", "consultas_no_disponibles", "estado", "alcance", "limitaciones",
    "application_name", "state", "sync_state", "sent_lsn", "write_lsn", "flush_lsn", "replay_lsn", "lag_bytes", "replay_lag_seconds",
    "status", "written_lsn", "flushed_lsn", "latest_end_lsn", "last_msg_receipt_time", "recibido_lsn", "aplicado_lsn", "ultimo_replay", "replay_pausado",
    "actual_lsn", "persistido_lsn", "archived_count", "failed_count", "last_archived_time", "last_failed_time", "num_timed", "num_requested", "write_time", "sync_time",
    "buffers_written", "stats_reset", "checkpoint_lsn", "redo_lsn", "timeline_id", "checkpoint_time", "total", "activas", "maximas", "fsync", "full_page_writes", "synchronous_commit", "wal_level", "archive_mode",
    "identidad", "identidad_nodo", "direccion", "puerto", "sistema", "identificador_sistema", "synchronous_standby_names", "transaction_read_only"}
METRIC_KEYS = {"disponible", "servicio", "instancia", "desde", "alcance", "solicitudes", "exitosas", "errores_http", "latencia_media_ms", "latencia_max_ms", "recientes", "momento", "correlation_id", "metodo", "ruta", "status", "latencia_ms"}


def whitelist(value, keys, depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {k: whitelist(v, keys, depth + 1) for k, v in value.items() if k in keys}
    if isinstance(value, list):
        return [whitelist(v, keys, depth + 1) for v in value[:100]]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return clean(value) if isinstance(value, str) else value if isinstance(value, (bool, int, float)) else None


def node_snapshot(events_url, authorization, *, include_business_inventory=True):
    document, warnings = node_operations.load_inventory()
    if not document:
        return {"capturado_en": now().isoformat(), "componentes": [], "persistencia": [], "metricas": [],
            "inventario": {"verificable": False}, "infraestructura": {"modo": "inventario_invalido"}, "limitaciones": warnings}
    entries = node_operations.component_entries(document)
    deadline = time.monotonic() + 20
    # Rotar el inicio evita que los nodos al final de un inventario grande queden
    # siempre fuera de la captura cuando hay muchos destinos inaccesibles.
    offset = int(time.time() // 20) % len(entries) if entries else 0
    scheduled = entries[offset:] + entries[:offset]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda entry: probe(entry, authorization, deadline), scheduled))
    indexed = {item[0]["id"]: item for item in results}
    components, persistence, metrics = [], [], []
    for entry in entries:
        component, diagnostic, measurement = indexed[entry["id"]]
        components.append(component)
        if diagnostic:
            persistence.append({"componente": component["id"], **(whitelist(diagnostic["datos"], DIAGNOSTIC_KEYS)
                if diagnostic["ok"] and diagnostic["datos"].get("disponible") else {"disponible": False, "estado": "desconocido", "motivo": diagnostic["motivo"]})})
        if measurement:
            metrics.append({"componente": component["id"], **(whitelist(measurement["datos"], METRIC_KEYS)
                if measurement["ok"] and measurement["datos"].get("disponible") else {"disponible": False})})
    database_components, assignments = node_operations.database_components(document, components, persistence, now().isoformat())
    components.extend(database_components)
    infrastructure = node_operations.infrastructure_view(document, components, assignments)
    inventory = {"verificable": False, "motivo": "Consulta no realizada dentro del presupuesto temporal"}
    if include_business_inventory and time.monotonic() < deadline:
        result = fetch(events_url, "/inventario/estado", authorization, deadline=deadline)
        inventory_keys = {"eventos", "evento_id", "inicial", "disponible", "reservadas", "vendidas", "consistente", "ecuacion"}
        inventory = {"verificable": result["ok"], **whitelist(result["datos"], inventory_keys)}
    return {"capturado_en": now().isoformat(), "componentes": components, "persistencia": persistence, "metricas": metrics,
        "inventario": inventory, "infraestructura": infrastructure,
        "limitaciones": warnings + ["Inventario declarado: independencia física, red y capacidad de carga pendientes de comprobar en los equipos.",
            "HA operativa no verificada. No existe promoción automática ni elección de primario desde el panel; el quórum WAL síncrono no es un quórum de elección.",
            "Presupuesto de inicio de sondas: 20 segundos, máximo ocho consultas concurrentes; destinos omitidos permanecen desconocidos y el orden inicial rota.",
            "Una respuesta HTTP no prueba continuidad de escrituras; PostgreSQL síncrono puede responder SELECT mientras sus commits esperan confirmaciones.",
            "Las proyecciones no ejecutan fallos; una falta de respuesta no confirma que el nodo esté apagado.",
            "Los standbys se relacionan con canales WAL por application_name; su salud física no se consulta directamente.",
            "El cambio de gateway usa una URL alternativa explícita y puede requerir iniciar sesión de nuevo.",
            "Lecturas tomadas en momentos distintos; el historial durable requiere capturas explícitas."]}


def snapshot(events_url, authorization):
    if node_operations.enabled():
        return node_snapshot(events_url, authorization)
    entries, warnings = topology(events_url)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda e: probe(e, authorization), entries))
    components, persistence, metrics = [], [], []
    for component, diagnostic, measurement in results:
        components.append(component)
        if diagnostic:
            persistence.append({"componente": component["id"], **(whitelist(diagnostic["datos"], DIAGNOSTIC_KEYS)
                if diagnostic["ok"] and diagnostic["datos"].get("disponible") else {"disponible": False, "estado": "desconocido", "motivo": "Diagnóstico no verificable; comprobar servicio, permisos y versión"})})
        if measurement:
            metrics.append({"componente": component["id"], **(whitelist(measurement["datos"], METRIC_KEYS)
                if measurement["ok"] and measurement["datos"].get("disponible") else {"disponible": False})})
    try:
        node_config = json.loads(os.getenv("NEXUSTIX_OPERATIONS_TOPOLOGY", "{}"))
        if not isinstance(node_config, dict):
            node_config = {}
    except ValueError:
        node_config = {}
    for domain in ("usuarios", "eventos", "compras"):
        sources = [d for d in persistence if d["componente"].startswith(domain)]
        available = [d for d in sources if d.get("disponible")]
        health_sources = [c for c in components if c["id"].startswith(domain) and c["tipo"] == "api"]
        known_failure = any(c["base_datos"] == "no_disponible" for c in health_sources)
        database_config = node_config.get(domain + "_db", {})
        database_node = clean(database_config.get("nodo"), 80) or None if isinstance(database_config, dict) else None
        components.append({"id": domain + "_db", "nombre": "PostgreSQL " + domain, "tipo": "postgresql",
            "nodo": database_node, "dependencias": [], "estado": "degradado" if known_failure else "saludable" if available else "desconocido",
            "comprobado_en": now().isoformat(), "latencia_ms": None, "instancia": None, "base_datos": "disponible" if available else "desconocido",
            "detalle": "Observado a través de las conexiones de las API; no es una sonda directa al contenedor",
            "origen_configuracion": "dependencia_api", "roles_observados": sorted({d["rol"] for d in available if d.get("rol")}),
            "replicas_observadas": sum(len(d.get("replicas") or []) for d in available[:1])})
        observed_replicas = next((d.get("replicas") for d in available if d.get("replicas")), [])
        for index, replica in enumerate(observed_replicas[:4]):
            components.append({"id": f"{domain}_standby_{index + 1}", "nombre": f"Standby observado · {domain} {index + 1}",
                "tipo": "replica_postgresql", "nodo": None, "dependencias": [domain + "_db"], "estado": "desconocido",
                "comprobado_en": now().isoformat(), "latencia_ms": None, "instancia": replica.get("application_name"),
                "base_datos": "desconocido", "origen_configuracion": "pg_stat_replication",
                "replicacion": replica.get("state"), "lag_bytes": replica.get("lag_bytes"),
                "detalle": "Canal de replicación observado desde el nodo conectado; no se consulta directamente salud ni equipo del standby."})
    events_target = next((e["url"] for e in entries if e["id"] == "eventos_entrada"), events_url)
    inventory_result = fetch(events_target, "/inventario/estado", authorization)
    inventory_keys = {"eventos", "evento_id", "inicial", "disponible", "reservadas", "vendidas", "consistente", "ecuacion"}
    inventory = {"verificable": inventory_result["ok"], **whitelist(inventory_result["datos"], inventory_keys)}
    return {"capturado_en": now().isoformat(), "componentes": components, "persistencia": persistence, "metricas": metrics, "inventario": inventory,
        "limitaciones": warnings + ["LAN: PostgreSQL está concentrado en PC2; no existe replicación entre las tres PC en el despliegue mínimo.",
            "Las URL por convención local son destinos candidatos; no certifican el despliegue ni su equipo físico.",
            "Usuarios B y Eventos B requieren configuración explícita; desconocido nunca significa saludable ni PC caída.",
            "Las réplicas PostgreSQL solo se informan cuando las consultas ofrecen evidencia; no se promocionan.",
            "Lecturas de servicios tomadas en momentos diferentes; no constituyen una transacción distribuida.",
            "Los GET no registran incidentes. Solo capturas explícitas conservan transiciones observadas."]}


def transitions(db, report):
    from shared.operations_models import IncidenteOperacion, ObservacionComponente
    # Serializa capturas entre réplicas; nunca ejecutado por endpoints GET.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(7419251055)"))
    moment = datetime.fromisoformat(report["fecha"])
    components = {c["id"]: c for c in report["evidencias"]["componentes"]}
    routed_peers = {"compras1": ("gateway", "compras2"), "compras2": ("gateway", "compras1"),
                    "usuarios_a": ("usuarios_entrada", "usuarios_b"), "usuarios_b": ("usuarios_entrada", "usuarios_a"),
                    "eventos_a": ("eventos_entrada", "eventos_b"), "eventos_b": ("eventos_entrada", "eventos_a")}
    for component in report["evidencias"]["componentes"]:
        key, state = component["id"], component["estado"]
        previous = db.get(ObservacionComponente, key)
        if previous and previous.capturado_en.replace(tzinfo=previous.capturado_en.tzinfo or timezone.utc) >= moment:
            continue
        old = previous.estado if previous else None
        incident = db.get(IncidenteOperacion, previous.incidente_id) if previous and previous.incidente_id else None
        continuing = None
        if component.get("origen_configuracion") == "inventario_nodos" and component.get("tipo") == "api":
            peer_instances = {c.get("instancia") for c in components.values() if c.get("tipo") == "api"
                and c.get("servicio") == component.get("servicio") and c["id"] != key and c.get("estado") == "saludable" and c.get("instancia")}
            for route in components.values():
                traffic = route.get("solicitud_observada", {})
                if (route.get("tipo") == "ruta" and route.get("servicio") == component.get("servicio")
                        and traffic.get("estado") == "saludable" and traffic.get("instancia") in peer_instances):
                    continuing = traffic["instancia"]
                    break
        elif key in routed_peers:
            route_id, peer_id = routed_peers[key]
            traffic = components.get(route_id, {}).get("solicitud_observada", {})
            peer = components.get(peer_id, {})
            if (traffic.get("estado") == "saludable" and peer.get("estado") == "saludable"
                    and peer.get("instancia") and traffic.get("instancia") == peer["instancia"]):
                continuing = peer["instancia"]
        if old == "saludable" and state in {"degradado", "desconocido"} and not incident:
            iid = uuid4().hex
            doc = {"id": iid, "componente": key, "detectado_en": moment.isoformat(), "estado_anterior": old,
                "ultima_captura_saludable": previous.capturado_en.isoformat(),
                "estado_actual": state, "tipo_fallo": "observacion_interrumpida_causa_desconocida" if state == "desconocido" else "dependencia_bd" if component.get("base_datos") == "no_disponible" else "respuesta_http_degradada",
                "alcance": "observado desde el servicio Compras; causa física no confirmada",
                "instancia_continuadora": continuing, "recuperado_en": None, "indisponibilidad_segundos": None,
                "ventana_observada_segundos": None, "evidencias": [report["id"]],
                "limitaciones": "Muestreo manual: no mide inicio exacto ni continuidad de la interrupción. Desconocido registra pérdida de observación, no una caída confirmada."}
            incident = IncidenteOperacion(id=iid, componente=key, detectado_en=moment, documento=doc)
            db.add(incident)
        elif incident and state != "saludable":
            incident.documento = {**incident.documento, "estado_actual": state,
                "instancia_continuadora": continuing or incident.documento.get("instancia_continuadora"),
                "evidencias": (incident.documento["evidencias"] + [report["id"]])[-100:]}
        elif incident and state == "saludable":
            detected = incident.detectado_en.replace(tzinfo=incident.detectado_en.tzinfo or timezone.utc)
            incident.recuperado_en = moment
            incident.documento = {**incident.documento, "estado_actual": state,
                "recuperado_en": moment.isoformat(), "ventana_observada_segundos": round((moment - detected).total_seconds(), 2),
                "evidencias": incident.documento["evidencias"] + [report["id"]]}
            incident = None
        if previous is None:
            previous = ObservacionComponente(componente=key)
            db.add(previous)
        previous.estado, previous.capturado_en = state, moment
        previous.incidente_id = incident.id if incident else None
        previous.documento = {"evidencia_id": report["id"], "instancia": component.get("instancia")}


def report_document(data, observation):
    scenario = next(row for row in scenarios() if row["id"] == data.escenario)
    return {"id": uuid4().hex, "formato": "nexustix-evidencia-v1", "escenario": data.escenario,
        "nombre": scenario["nombre"], "fecha": observation["capturado_en"], "objetivo": scenario["objetivo"],
        "unidad_academica": "Unidad II", "conceptos": scenario["conceptos"], "componentes": scenario["componentes"],
        "acciones": clean(data.acciones), "esperado": scenario["esperado"], "observado": clean(data.observado, 2000),
        "evidencias": observation, "limitaciones": scenario["limitaciones"] + observation.get("limitaciones", []),
        "conclusion": clean(data.conclusion), "resultado": "sin_evaluar", "origen": "captura",
        "fase": getattr(data, "fase", "observacion"), "referencia_inicial": getattr(data, "referencia_inicial", None),
        "ventana_observacion_segundos": None}


def evidence_phase(label, document):
    evidence = document.get("evidencias") if isinstance(document.get("evidencias"), dict) else {}
    rows = [row for row in evidence.get("componentes") or [] if isinstance(row, dict) and row.get("tipo") != "ruta"]
    silent = lambda row: row.get("sonda_realizada") is True and row.get("http_status") is None
    pick = lambda test: [row.get("id") for row in rows if test(row)]
    return {"fase": label, "fecha": document.get("fecha"), "saludables": pick(lambda row: row.get("estado") == "saludable"),
        "degradados": pick(lambda row: row.get("estado") in {"degradado", "caido"}), "sin_respuesta": pick(silent),
        "desconocidos": pick(lambda row: row.get("estado") == "desconocido" and not silent(row)), "pendientes": evidence.get("pendientes")}


def elapsed_seconds(start, end):
    try:
        first, last = [value if isinstance(value, datetime) else datetime.fromisoformat(str(value)) for value in (start, end)]
        return round((last - first).total_seconds(), 2)
    except (TypeError, ValueError):
        return None


def experiment_results(db, models_module, report, initial, failure, data):
    """Compara capturas reales; todo lo aportado por el operador queda etiquetado como tal."""
    measures = data.mediciones or OperatorMeasures()
    phases = [evidence_phase("Estado inicial", initial)]
    if failure:
        phases.append(evidence_phase("Posterior a la falla", failure))
    closing = evidence_phase("Posterior a la recuperación" if failure else "Captura final", report)
    phases.append(closing)
    faulted = phases[1] if failure else closing
    rows = []

    def add(group, metric, value, kind, note=""):
        rows.append({"grupo": group, "metrica": metric, "valor": value, "tipo": kind, "nota": note})

    faulty = bool(faulted["degradados"] or faulted["sin_respuesta"])
    add("Disponibilidad", measures.operacion or "Operación", f"{measures.correctas or 0} correctas · {measures.fallidas or 0} fallidas de {measures.intentadas}" if measures.intentadas else "Sin medición", "medicion_declarada" if measures.intentadas else "desconocido")
    add("Detección", "Última observación saludable", initial.get("fecha") if phases[0]["saludables"] else "Sin medición", "medicion" if phases[0]["saludables"] else "desconocido")
    add("Detección", "Primera observación con fallo", faulted["fecha"] if faulty else "Sin medición", "medicion" if faulty else "desconocido", "La falla pudo comenzar antes de esta captura." if faulty else "")
    window = elapsed_seconds(initial.get("fecha"), faulted["fecha"])
    add("Detección", "Ventana entre observaciones", f"{window} s" if window is not None else "No determinable", "medicion" if window is not None else "desconocido", "Es una ventana, no un tiempo de detección.")
    detection = elapsed_seconds(measures.inicio_falla, faulted["fecha"]) if measures.inicio_falla and faulty else None
    add("Detección", "Tiempo hasta la primera observación", f"{detection} s" if detection is not None else "No determinable", "estimacion" if detection is not None else "desconocido", "Usa la marca de inicio declarada por el operador." if detection is not None else "Requiere una marca fiable del inicio.")
    add("Recuperación", "Momento de la falla", measures.inicio_falla.isoformat() if measures.inicio_falla else "Desconocido", "medicion_declarada" if measures.inicio_falla else "desconocido")
    restored = not closing["degradados"] and not closing["sin_respuesta"] and bool(closing["saludables"]) and bool(failure)
    add("Recuperación", "Servicios observados como recuperados", closing["fecha"] if restored else "Sin medición", "medicion" if restored else "desconocido", "Los servicios responden; las funciones comerciales requieren su propia comprobación.")
    add("RTO", "Objetivo", f"{measures.rto_objetivo_segundos} s" if measures.rto_objetivo_segundos is not None else "No declarado", "objetivo" if measures.rto_objetivo_segundos is not None else "desconocido")
    rto = elapsed_seconds(measures.inicio_falla, measures.restauracion) if measures.inicio_falla and measures.restauracion else None
    add("RTO", "Tiempo de recuperación", f"{rto} s" if rto is not None else "Sin medición", "estimacion" if rto is not None else "desconocido", "Calculado con las marcas declaradas por el operador." if rto is not None else "Dos capturas aisladas no determinan un RTO.")
    add("RPO", "Objetivo", measures.rpo_objetivo or "No declarado", "objetivo" if measures.rpo_objetivo else "desconocido")
    add("RPO", "Última transacción reconocida", measures.ultima_transaccion_reconocida or "No determinable", "medicion_declarada" if measures.ultima_transaccion_reconocida else "desconocido")
    add("RPO", "Última transacción recuperada", measures.ultima_transaccion_recuperada or "No determinable", "medicion_declarada" if measures.ultima_transaccion_recuperada else "desconocido")
    add("RPO", "Pérdida observada", measures.perdida_observada or "No determinable", "medicion_declarada" if measures.perdida_observada else "desconocido", "Con replicación asíncrona no se presume pérdida cero.")
    source = (failure or report).get("evidencias") or {}
    latencies = [row.get("latencia_ms") for row in source.get("componentes") or [] if isinstance(row, dict) and isinstance(row.get("latencia_ms"), (int, float))]
    add("Latencia", "Máxima en la captura", f"{max(latencies)} ms" if latencies else "Sin medición", "medicion" if latencies else "desconocido")
    add("Latencia", "Respuestas lentas (más de 800 ms)", sum(value > 800 for value in latencies) if latencies else "Sin medición", "medicion" if latencies else "desconocido")
    add("Latencia", "Sondas sin respuesta", len(faulted["sin_respuesta"]), "medicion")
    closing_evidence = report.get("evidencias") or {}
    lsns = [f"{row.get('componente')}: {row['wal']['actual_lsn']}" for row in closing_evidence.get("persistencia") or [] if isinstance(row, dict) and isinstance(row.get("wal"), dict) and row["wal"].get("actual_lsn")]
    add("Replicación", "LSN actual", " · ".join(lsns[:6]) or "Sin medición", "medicion" if lsns else "desconocido")
    lags = [standby.get("lag_bytes") for base in (closing_evidence.get("infraestructura") or {}).get("bases", []) for standby in base.get("standbys", []) if isinstance(standby.get("lag_bytes"), (int, float))]
    add("Replicación", "Mayor diferencia WAL", f"{max(lags)} bytes" if lags else "No determinable", "medicion" if lags else "desconocido", "Bytes de WAL, no tiempo.")
    inventory = closing_evidence.get("inventario") or {}
    add("Consistencia", "Inventario", "Consulta no disponible" if not inventory.get("verificable") else "Consistente" if inventory.get("consistente") is True else "Inconsistente o no confirmado", "medicion" if inventory.get("verificable") else "desconocido", "Solo describe esta captura.")
    report["comparacion"], report["metricas"], report["pasos_recuperacion"] = phases, rows, list(data.pasos_recuperacion)
    report["mediciones_operador"] = measures.model_dump(mode="json")
    infrastructure = closing_evidence.get("infraestructura") or {}
    since = datetime.fromisoformat(initial["fecha"])
    report["experimento"] = {"id": report["id"], "perfil": infrastructure.get("perfil"), "nodos": [row.get("id") for row in infrastructure.get("nodos", [])],
        "nodo_objetivo": data.nodo_objetivo, "incidentes": db.query(models_module).filter(models_module.detectado_en >= since).count()}
    report["origen_datos"] = {"capturas": "Consultas del servidor en cada etapa", "mediciones_operador": "Declaradas por quien ejecutó el experimento", "proyeccion": "Inventario de la captura inicial"}
    expected_source = initial.get("evidencias") or {}
    projection = next((row for row in (expected_source.get("infraestructura") or {}).get("proyecciones", []) if row.get("tipo") == "nodo" and row.get("objetivo") == data.nodo_objetivo), None)
    if projection:
        expected = sorted(row.get("id") for row in expected_source.get("componentes") or [] if isinstance(row, dict) and row.get("nodo") == data.nodo_objetivo and row.get("tipo") != "ruta")
        observed = sorted(set(faulted["degradados"]) | set(faulted["sin_respuesta"]))
        report["proyeccion_vs_observado"] = {"nodo": data.nodo_objetivo, "previstos": expected, "observados": observed,
            "previstos_no_observados": sorted(set(expected) - set(observed)), "observados_no_previstos": sorted(set(observed) - set(expected))}


def install(app, models, get_db, require_admin, events_url):
    # Sólo Compras registra estos modelos. Los observadores de Usuarios/Eventos
    # reutilizan sondas HTTP sin agregar tablas a sus metadatos de negocio.
    from shared.operations_models import EvidenciaOperacion, IncidenteOperacion
    @app.get("/admin/operaciones/estado")
    def state(authorization: str | None = Header(default=None), db=Depends(get_db), _admin=Depends(require_admin)):
        observation = snapshot(events_url, authorization)
        observation["pendientes"] = pending_values(db, models)
        return observation

    @app.get("/admin/operaciones/escenarios")
    def scenario_list(_admin=Depends(require_admin)):
        return {"escenarios": scenarios()}

    @app.get("/admin/operaciones/incidentes")
    def incidents(limit: int = Query(default=50, ge=1, le=200), db=Depends(get_db), _admin=Depends(require_admin)):
        rows = db.query(IncidenteOperacion).order_by(IncidenteOperacion.detectado_en.desc()).limit(limit).all()
        return {"incidentes": [r.documento for r in rows], "alcance": "Transiciones verificables de capturas explícitas; desconocido no prueba una caída."}

    @app.get("/admin/operaciones/evidencias")
    def evidence_list(limit: int = Query(default=50, ge=1, le=200), db=Depends(get_db), _admin=Depends(require_admin)):
        rows = db.query(EvidenciaOperacion).order_by(EvidenciaOperacion.creado_en.desc()).limit(limit).all()
        keys = {"id", "escenario", "nombre", "fecha", "fase", "origen", "resultado", "referencia_inicial"}
        return {"evidencias": [{k: v for k, v in row.documento.items() if k in keys} for row in rows]}

    @app.post("/admin/operaciones/evidencias", status_code=201)
    def capture(data: CaptureRequest, authorization: str | None = Header(default=None), db=Depends(get_db), _admin=Depends(require_admin)):
        initial = None
        if data.fase == "final" and not data.referencia_inicial:
            raise HTTPException(422, "La captura final requiere referencia_inicial")
        if data.referencia_inicial:
            initial = db.get(EvidenciaOperacion, data.referencia_inicial)
            if not initial or initial.escenario != data.escenario or initial.documento.get("fase") != "inicial" or initial.documento.get("origen") != "captura":
                raise HTTPException(422, "Referencia inicial no corresponde a una captura real del mismo escenario")
        failure = None
        if data.referencia_falla:
            failure = db.get(EvidenciaOperacion, data.referencia_falla)
            if data.fase != "final" or not initial or not failure or failure.escenario != data.escenario or failure.documento.get("fase") != "observacion" or failure.documento.get("referencia_inicial") != data.referencia_inicial:
                raise HTTPException(422, "Referencia de falla no corresponde a una observación de este experimento")
        moments = [data.mediciones.inicio_falla, data.mediciones.restauracion] if data.mediciones else []
        if any(moment is not None and moment.tzinfo is None for moment in moments):
            raise HTTPException(422, "Las fechas declaradas requieren zona horaria")
        observation = snapshot(events_url, authorization)
        if node_operations.enabled():
            from shared.cluster_observer import observer
            observation = observer.record(observation)
        report = report_document(data, observation)
        report["evidencias"]["pendientes"] = pending_values(db, models)
        if initial:
            report["estado_inicial"] = initial.documento["evidencias"]
            report["ventana_observacion_segundos"] = round((datetime.fromisoformat(report["fecha"]) - datetime.fromisoformat(initial.documento["fecha"])).total_seconds(), 2)
        transitions(db, report)
        if initial and data.fase == "final":
            experiment_results(db, IncidenteOperacion, report, initial.documento, failure.documento if failure else None, data)
        db.add(EvidenciaOperacion(id=report["id"], escenario=data.escenario, documento=report))
        db.commit()
        return report

    @app.post("/admin/operaciones/evidencias/importar", status_code=201)
    def import_evidence(data: ImportRequest, db=Depends(get_db), _admin=Depends(require_admin)):
        dates = [data.fecha, data.evidencias.capturado_en, *(c.comprobado_en for c in data.evidencias.componentes)]
        if any(d.tzinfo is None for d in dates):
            raise HTTPException(422, "Las fechas importadas requieren zona horaria")
        observation = data.evidencias.model_dump(mode="json")
        for component in observation["componentes"]:
            component["instancia"] = clean(component["instancia"], 80) or None
            component["nodo"] = clean(component["nodo"], 80) or None
        report = report_document(data, observation)
        report.update({"origen": "importada_no_verificada", "fecha": data.fecha.isoformat(), "importado_en": now().isoformat()})
        report["limitaciones"].append("Importación no verificada: solo observaciones tipadas de componentes; se descartan secretos, payloads arbitrarios y estadísticas no tipadas. No altera salud ni incidentes.")
        db.add(EvidenciaOperacion(id=report["id"], escenario=data.escenario, documento=report))
        db.commit()
        return report

    @app.get("/admin/operaciones/evidencias/{evidence_id}")
    def evidence(evidence_id: str, db=Depends(get_db), _admin=Depends(require_admin)):
        row = db.get(EvidenciaOperacion, evidence_id)
        if not row:
            raise HTTPException(404, "Evidencia no encontrada")
        return row.documento

    @app.get("/admin/operaciones/trazas/{identifier}")
    def trace(identifier: str, authorization: str | None = Header(default=None), db=Depends(get_db), _admin=Depends(require_admin)):
        return purchase_trace(db, models, identifier, events_url, authorization)

    @app.get("/admin/operaciones/pendientes")
    def pending(db=Depends(get_db), _admin=Depends(require_admin)):
        return pending_values(db, models)

    @app.get("/admin/operaciones/auditoria")
    def audit_log(limit: int = Query(default=100, ge=1, le=200), db=Depends(get_db), _admin=Depends(require_admin)):
        keys = ("accion", "recurso", "resultado", "actor", "rol", "operacion_id", "motivo", "correlation_id")
        rows = db.query(models.RegistroJournal).filter(models.RegistroJournal.tipo == "AUDITORIA").order_by(models.RegistroJournal.id.desc()).limit(limit).all()
        return {"registros": [{**{key: (row.detalles or {}).get(key) for key in keys}, "momento": iso_moment(row.timestamp), "instancia": row.instancia} for row in rows]}

    @app.get("/admin/operaciones/seguridad")
    def security_status(_admin=Depends(require_admin)):
        return security_values()

    @app.get("/admin/operaciones/outbox")
    def outbox(limit: int = Query(default=20, ge=1, le=100), db=Depends(get_db), _admin=Depends(require_admin)):
        return outbox_values(db, models, limit)

    @app.get("/admin/operaciones/concurrencia")
    def concurrency(evento_id: int | None = Query(default=None, gt=0), limit: int = Query(default=300, ge=1, le=500),
                    db=Depends(get_db), _admin=Depends(require_admin)):
        return concurrency_values(db, models, evento_id, limit)

    @app.get("/admin/operaciones/recuperacion")
    def recovery(limit: int = Query(default=50, ge=1, le=100), db=Depends(get_db), _admin=Depends(require_admin)):
        return recovery_values(db, models, limit)


def security_values():
    """Solo indica cómo está configurado cada secreto: nunca su valor ni su ruta."""
    def secret(label, name):
        path = os.getenv(name + "_FILE")
        return {"nombre": label, "fuente": "archivo" if path else "entorno" if os.getenv(name) else "no_configurado",
            "presente": os.path.isfile(path) if path else bool(os.getenv(name))}
    origins = [item.strip() for item in os.getenv("FRONTEND_ORIGINS", "").split(",") if item.strip()]
    portal = urlsplit(os.getenv("PUBLIC_PORTAL_URL", "")) if os.getenv("PUBLIC_PORTAL_URL") else None
    return {"secretos": [secret("Conexión a la base de Compras", "DATABASE_URL"), secret("Clave pública JWT", "JWT_PUBLIC_KEY"), secret("Token entre servicios", "EVENTOS_SERVICE_TOKEN")],
        "origenes_permitidos": len(origins), "origen_comodin": "*" in origins, "esquema_portal": portal.scheme if portal else None,
        "alcance": "Vista de esta instancia de Compras."}


def outbox_values(db, models, limit):
    """Lectura acotada del outbox; publicado_en solo acredita la notificación interna."""
    try:
        with db.begin_nested():
            rows = db.query(models.Outbox).order_by(models.Outbox.id.desc()).limit(limit).all()
            pending = db.query(models.Outbox).filter(models.Outbox.publicado_en.is_(None)).count()
    except SQLAlchemyError:
        return {"disponible": False, "registros": [], "nota": "Outbox no consultable; comprobar persistencia y migraciones."}
    def row_view(row):
        details = row.datos if isinstance(row.datos, dict) else {}
        return {"id": row.id, "tipo": clean(row.tipo, 40), "orden_id": row.orden_id, "productor": "Compras",
            "instancia": clean(details.get("instancia"), 80) or None,
            "registrado_en": row.creado_en.isoformat() if row.creado_en else None,
            "procesado_en": row.publicado_en.isoformat() if row.publicado_en else None,
            "estado": "notificacion_interna_registrada" if row.publicado_en else "pendiente"}
    return {"disponible": True, "pendientes": pending, "registros": [row_view(row) for row in rows],
        "nota": "El procesamiento registra una notificación en la misma base de Compras. No hay broker externo ni confirmación de lectura o entrega al comprador."}


def concurrency_values(db, models, event_id, limit):
    """Compara pedidos que apuntaron al mismo recurso sin exponer a los compradores."""
    reservations = {"reservada": "reservada", "pagada": "reservada", "emitiendo": "reservada", "completada": "confirmada"}
    try:
        with db.begin_nested():
            orders = db.query(models.Orden).order_by(models.Orden.creado_en.desc(), models.Orden.id).limit(limit).all()
    except SQLAlchemyError:
        return {"disponible": False, "grupos": [], "nota": "Pedidos no consultables; comprobar persistencia y migraciones."}
    groups = {}
    for order in orders:
        for item in order.items if isinstance(order.items, list) else []:
            if not isinstance(item, dict) or (event_id is not None and item.get("evento_id") != event_id):
                continue
            rejected = item.get("rechazo_reserva") if isinstance(item.get("rechazo_reserva"), dict) else {}
            conflict = rejected.get("status_http") == 409
            result = ("conflicto_disponibilidad" if conflict else "reserva_aceptada" if order.estado in reservations
                else "sin_resultado" if order.estado == "reservando" else "cancelada_o_compensada")
            operation = {"operacion_id": clean(item.get("operacion_id"), 64) or None,
                "inicio_registrado": order.creado_en.isoformat() if order.creado_en else None, "resultado": result,
                "conflicto": clean(rejected.get("detalle"), 200) if conflict else None,
                "estado_reserva": reservations.get(order.estado), "estado_final": order.estado, "motivo": clean(order.motivo, 60) or None}
            seats = item.get("asientos") if isinstance(item.get("asientos"), list) else []
            targets = [("asiento", clean(seat, 40)) for seat in seats[:100]]
            if not targets:
                targets = [("zona", clean(item.get("zona"), 80))] if item.get("zona") else [("general", "Admisión general")]
            for kind, label in targets:
                groups.setdefault((item.get("evento_id"), kind, label), []).append(operation)
    def evidence(rows):
        conflicts = any(row["resultado"] == "conflicto_disponibilidad" for row in rows)
        accepted = any(row["resultado"] == "reserva_aceptada" for row in rows)
        return "conflicto_con_contraparte" if conflicts and accepted else "conflicto_sin_contraparte" if conflicts else "mismo_recurso_sin_conflicto"
    comparable = [(key, rows) for key, rows in groups.items() if len(rows) > 1 or evidence(rows) != "mismo_recurso_sin_conflicto"]
    rank = {"conflicto_con_contraparte": 0, "conflicto_sin_contraparte": 1, "mismo_recurso_sin_conflicto": 2}
    comparable.sort(key=lambda entry: (rank[evidence(entry[1])], -len(entry[1])))
    return {"disponible": True, "pedidos_analizados": len(orders), "limite": limit, "grupos_totales": len(comparable),
        "grupos": [{"evento_id": key[0], "tipo_recurso": key[1], "recurso": key[2], "evidencia": evidence(rows), "total_operaciones": len(rows),
            "operaciones": sorted(rows, key=lambda row: row["inicio_registrado"] or "")[:10]} for key, rows in comparable[:30]],
        "limitaciones": ["La fecha es la creación persistida del pedido: no marca inicio ni fin de la petición y no prueba solapamiento temporal.",
            "Solo se analizan los pedidos más recientes dentro del límite; la contraparte de un conflicto puede quedar fuera.",
            "El estado de reserva se deduce del estado del pedido; el recorrido de cada operación consulta Eventos."]}


def iso_moment(value):
    return value.isoformat() if isinstance(value, datetime) else value


def stalled(moment):
    """Una transición normal tarda segundos; sin fecha tampoco se presume que avance."""
    if not isinstance(moment, datetime):
        return True
    return (now() - moment.replace(tzinfo=moment.tzinfo or timezone.utc)).total_seconds() > 60


def masked_key(value):
    return "…" + str(value)[-4:] if value else None


SAGA_FORWARD = {"reservada", "pagada", "emitiendo", "completada"}
SAGA_COMPENSATION = {"cancelando", "cancelada", "reembolsando", "reembolsada"}
PENDING_STEPS = {"reservando": "Verificar o solicitar la reserva en Eventos", "pagada": "Confirmar la reserva en Eventos",
    "emitiendo": "Emitir las entradas", "cancelando": "Liberar la reserva en Eventos", "reembolsando": "Liberar la reserva y cerrar el reembolso"}
COMPENSATION_REASONS = {"pago_rechazado": "Pago simulado rechazado.", "reserva_expirada": "Reserva vencida.",
    "inventario_no_disponible": "Eventos rechazó una reserva del pedido.", "cancelada_por_usuario": "Cancelación solicitada para el pedido.",
    "evento_cancelado": "Evento cancelado."}


def saga_order_view(order, transitions, remote_states, payment):
    items = [item for item in order.items if isinstance(item, dict)] if isinstance(order.items, list) else []
    rejected = next((item["rechazo_reserva"] for item in items if isinstance(item.get("rechazo_reserva"), dict)), None)
    latest = max((row.creado_en for row in transitions.values() if row.creado_en), default=order.creado_en)
    waiting = "recuperacion_requerida" if stalled(latest) else "pendiente"
    known = [remote_states.get(item.get("operacion_id"), "sin_consulta") for item in items]

    def step(name, state, evidence, kind=None):
        row = transitions.get(kind) if kind else None
        details = row.datos if row is not None and isinstance(row.datos, dict) else {}
        return {"paso": name, "estado": state, "evidencia": clean(evidence, 300), "momento": iso_moment(row.creado_en) if row is not None else None,
            "origen": None if row is None else "solicitud_http" if details.get("correlation_id") else "trabajo_recuperacion"}

    steps = [step("Solicitud recibida", "completado", "Existe un pedido persistido para la solicitud.", "reservando"),
        step("Operación identificada con clave idempotente", "completado", f"{len(items)} operación(es) derivadas de una clave durable.", "reservando"),
        step("Pedido persistido en Compras", "completado", "Compras registra el pedido antes de contactar a Eventos.", "reservando")]
    if rejected:
        steps += [step("Reserva solicitada a Eventos", "completado", "Eventos respondió y rechazó la reserva."),
            step("Inventario reservado", "fallido", rejected.get("detalle") or f"HTTP {rejected.get('status_http')}")]
    elif "reservada" in transitions:
        steps += [step("Reserva solicitada a Eventos", "completado", "Compras recibió y validó la respuesta de Eventos.", "reservada"),
            step("Inventario reservado", "completado", "Reserva aceptada y registrada por Compras.", "reservada")]
    elif order.estado == "reservando":
        accepted = any(value in {"reservada", "confirmada"} for value in known)
        absent = bool(known) and all(value == "ausente" for value in known)
        steps += [step("Reserva solicitada a Eventos", "desconocido", "Resultado remoto pendiente de verificación."),
            step("Inventario reservado", "recuperacion_requerida" if accepted else waiting if absent else "desconocido",
                "Eventos registra la reserva; Compras todavía no persiste su resultado." if accepted
                else "Eventos no registra esta reserva en esta consulta." if absent else "Resultado remoto pendiente de verificación.")]
    else:
        steps.append(step("Inventario reservado", "desconocido", "No hay registro de una reserva aceptada."))
    approved = payment is not None and payment.estado in {"aprobado", "reembolsado", "reembolsado_parcial"}
    compensating = order.estado in SAGA_COMPENSATION
    if payment is not None:
        steps.append(step("Pago simulado", "completado" if approved else "fallido", f"Pago registrado: {payment.estado}.", "pagada" if approved else None))
    elif order.estado == "reservada":
        steps.append(step("Pago simulado", "pendiente", "Reserva vigente a la espera del pago."))
    if "emitiendo" in transitions:
        steps.append(step("Confirmación de reserva", "completado", "Eventos confirmó la reserva y Compras lo validó.", "emitiendo"))
    elif not compensating:
        steps.append(step("Confirmación de reserva", waiting if order.estado == "pagada" else "pendiente", "Todavía no se registra la confirmación."))
    if "completada" in transitions:
        steps += [step("Emisión de entradas", "completado", "Entradas emitidas.", "completada"),
            step("Pedido completado", "completado", "Cierre registrado por Compras.", "completada")]
    elif not compensating:
        steps += [step("Emisión de entradas", waiting if order.estado == "emitiendo" else "pendiente", "Todavía no se registra la emisión."),
            step("Pedido completado", "pendiente", "Todavía no se registra el cierre.")]
    compensation = []
    started = next((kind for kind in ("cancelando", "reembolsando") if kind in transitions), None)
    if started or compensating:
        compensation.append(step("Compensación iniciada", "completado", COMPENSATION_REASONS.get(order.motivo, f"Motivo registrado: {order.motivo or 'no informado'}."), started))
        done = next((kind for kind in ("cancelada", "reembolsada") if kind in transitions), None)
        released = bool(known) and all(value == "liberada" for value in known)
        compensation.append(step("Reserva liberada en Eventos", "compensado" if done else waiting,
            "Compras recibió la confirmación de liberación." if done else "Eventos registra la liberación; Compras aún debe cerrar el pedido." if released else "Liberación aún no confirmada.", done))
        if "reembolsando" in transitions:
            refunded = payment is not None and str(payment.estado).startswith("reembolsado")
            compensation.append(step("Reembolso simulado", "compensado" if refunded else waiting, f"Pago registrado: {payment.estado if payment is not None else 'sin registro'}."))
    every = steps + compensation
    return {"tipo": "pedido", "pasos": steps, "compensaciones": compensation, "incierto": any(row["estado"] in {"desconocido", "recuperacion_requerida"} for row in every)}


def saga_purchase_view(purchases, claims, remote_states):
    steps = []
    for claim in claims[:20]:
        steps.append({"paso": "Solicitud identificada con clave idempotente", "estado": "completado" if claim.state == "completed" else "recuperacion_requerida" if stalled(claim.created_at) else "pendiente",
            "evidencia": "Respuesta final persistida." if claim.response_status else "Respuesta final aún no persistida.", "momento": iso_moment(claim.created_at), "origen": None})
    for operation, state in list(remote_states.items())[:20]:
        unknown = state in {None, "ausente"}
        steps.append({"paso": "Reserva en Eventos", "estado": "desconocido" if unknown else {"liberada": "compensado", "reservada": "pendiente"}.get(state, "completado"),
            "evidencia": "Resultado remoto pendiente de verificación." if state is None else "Eventos no registra esta reserva." if state == "ausente" else f"Eventos registra: {state}.", "momento": None, "origen": None})
    for purchase in purchases[:20]:
        steps.append({"paso": "Compra persistida", "estado": "completado" if purchase.estado == "confirmada" else "desconocido",
            "evidencia": f"Estado registrado: {clean(purchase.estado, 40)}.", "momento": iso_moment(purchase.creado_en), "origen": None})
    return {"tipo": "compra_directa", "pasos": steps, "compensaciones": [], "incierto": any(row["estado"] in {"desconocido", "recuperacion_requerida"} for row in steps)}


def recovery_values(db, models, limit):
    """Foto de solo lectura: el trabajo de recuperación existente sigue siendo el único que actúa."""
    try:
        with db.begin_nested():
            orders = db.query(models.Orden).filter(models.Orden.estado.in_(list(PENDING_STEPS))).order_by(models.Orden.creado_en).limit(limit).all()
            ids = [order.id for order in orders]
            latest = dict(db.query(models.Outbox.orden_id, func.max(models.Outbox.creado_en)).filter(models.Outbox.orden_id.in_(ids)).group_by(models.Outbox.orden_id).all()) if ids else {}
            reserved = {row[0] for row in db.query(models.Outbox.orden_id).filter(models.Outbox.orden_id.in_(ids), models.Outbox.tipo == "reservada")} if ids else set()
            claims = db.query(models.IdempotencyRequest).filter_by(state="processing").order_by(models.IdempotencyRequest.created_at).limit(limit).all()
            recent = db.query(models.Outbox).order_by(models.Outbox.id.desc()).limit(300).all()
            recovered_ids = list({row.orden_id for row in recent if row.tipo in (SAGA_FORWARD | SAGA_COMPENSATION) and not (row.datos or {}).get("correlation_id")})[:20]
            recovered = {order.id: order for order in db.query(models.Orden).filter(models.Orden.id.in_(recovered_ids))} if recovered_ids else {}
    except SQLAlchemyError:
        return {"disponible": False, "pendientes": [], "recuperadas": [], "nota": "Pedidos no consultables; comprobar persistencia y migraciones."}
    pending = [{"tipo": "pedido", "referencia": order.id, "orden_id": order.id,
        "operaciones": [clean(item.get("operacion_id"), 64) for item in order.items if isinstance(item, dict)][:5] if isinstance(order.items, list) else [],
        "ultimo_estado": order.estado, "motivo": clean(order.motivo, 60) or None,
        "reserva_remota": "Aceptada según Compras" if order.id in reserved else "Sin constancia en Compras; consulte el recorrido",
        "paso_pendiente": PENDING_STEPS[order.estado], "modalidad": "compensacion" if order.estado in SAGA_COMPENSATION else "adelante",
        "intervencion": "Revisar: más de 60 s sin una transición nueva" if stalled(latest.get(order.id) or order.creado_en) else "No indicada",
        "actualizado_en": iso_moment(latest.get(order.id) or order.creado_en)} for order in orders]
    pending += [{"tipo": "compra_directa", "referencia": claim.operacion_id, "orden_id": None, "operaciones": [claim.operacion_id],
        "ultimo_estado": "processing", "motivo": None, "reserva_remota": "Sin constancia en Compras; consulte el recorrido",
        "paso_pendiente": "Determinar el resultado de la compra directa", "modalidad": "por_determinar",
        "intervencion": "Revisar: más de 60 s sin respuesta persistida" if stalled(claim.created_at) else "No indicada",
        "actualizado_en": iso_moment(claim.created_at)} for claim in claims]
    history = []
    for order_id, order in recovered.items():
        rows = sorted((row for row in recent if row.orden_id == order_id and row.tipo in (SAGA_FORWARD | SAGA_COMPENSATION | {"reservando"})), key=lambda row: row.id)
        first = next((index for index, row in enumerate(rows) if not (row.datos or {}).get("correlation_id")), None)
        if first is None:
            continue
        history.append({"orden_id": order.id, "estado_anterior": rows[first - 1].tipo if first else None, "accion_observada": rows[first].tipo,
            "estado_posterior": order.estado, "modalidad": "compensacion" if rows[first].tipo in SAGA_COMPENSATION else "adelante",
            "secuencia": [row.tipo for row in rows], "actualizado_en": iso_moment(rows[-1].creado_en)})
    history.sort(key=lambda row: row["actualizado_en"] or "", reverse=True)
    return {"disponible": True, "pendientes": pending, "recuperadas": history, "consultado_en": now().isoformat(),
        "nota": "Una transición sin correlación HTTP la registró un trabajo en segundo plano. Solo se revisan los 300 eventos más recientes del outbox."}


def pending_values(db, models):
    try:
        with db.begin_nested():
            return {"disponible": True,
                "ordenes": {state: count for state, count in db.query(models.Orden.estado, func.count()).group_by(models.Orden.estado)},
                "outbox_pendiente": db.query(models.Outbox).filter(models.Outbox.publicado_en.is_(None)).count(),
                "idempotencia_en_proceso": db.query(models.IdempotencyRequest).filter_by(state="processing").count(),
                "cancelaciones": {state: count for state, count in db.query(models.CancelacionEvento.estado, func.count()).group_by(models.CancelacionEvento.estado)},
                "reembolsos": {state: count for state, count in db.query(models.ReembolsoEvento.estado, func.count()).group_by(models.ReembolsoEvento.estado)} if hasattr(models, "ReembolsoEvento") else None,
                "reintentos": None, "nota": "No existe contador durable de todos los reintentos; no se estima a partir del estado."}
    except SQLAlchemyError:
        return {"disponible": False, "nota": "Persistencia no consultable; comprobar disponibilidad y migraciones pendientes."}


def purchase_trace(db, models, identifier, events_url, authorization):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
        raise HTTPException(422, "Identificador de orden, compra u operación inválido")
    order = db.get(models.Orden, identifier)
    purchase = db.get(models.Compra, int(identifier)) if identifier.isdigit() and len(identifier) < 12 else None
    if not order and not purchase:
        purchase = db.query(models.Compra).filter_by(operacion_id=identifier).first()
    if not order and purchase:
        ticket = db.query(models.Entrada).filter_by(compra_id=purchase.id).first()
        order = db.get(models.Orden, ticket.orden_id) if ticket else None
    if not order and identifier.startswith("ord-"):
        candidate = identifier.split("-")
        order = db.get(models.Orden, candidate[1]) if len(candidate) == 3 else None
    operation = db.query(models.IdempotencyRequest).filter_by(operacion_id=identifier).first()
    if not order and not purchase and not operation:
        raise HTTPException(404, "No se encontró una operación persistida con ese identificador")
    timeline, correlations = [], set()

    def add(moment, stage, source, state, instance=None, detail="", clock=None, local=None, relation=None):
        timeline.append({"momento": moment.isoformat() if isinstance(moment, datetime) else moment,
            "etapa": clean(stage, 100), "fuente": source, "estado": clean(state, 80),
            "instancia": clean(instance, 80) or None, "detalle": clean(detail, 600),
            # Los pasos de la Saga guardan reloj_t=0: ausencia de reloj, no un valor Lamport.
            "reloj_logico": clock if isinstance(clock, int) and clock > 0 else None,
            "orden_local": local, "relacion": clean(relation, 200) or None})

    operation_ids, purchases = [], []
    transitions, payment = {}, None
    # Estas transiciones solo se persisten tras validar la respuesta correspondiente de Eventos.
    causes = {"reservada": "Posterior a la respuesta validada de Eventos a la reserva", "emitiendo": "Posterior a la respuesta validada de Eventos a la confirmación",
        "cancelada": "Posterior a la respuesta validada de Eventos a la liberación", "reembolsada": "Posterior a la respuesta validada de Eventos a la liberación"}
    if order:
        add(order.creado_en, "Solicitud persistida", "ordenes", order.estado, detail="Estado actual de la orden; la fecha corresponde a su creación.")
        operation_ids = [item["operacion_id"] for item in order.items if isinstance(item, dict) and item.get("operacion_id")]
        purchases = db.query(models.Compra).filter(models.Compra.operacion_id.in_(operation_ids)).all() if operation_ids else []
        for item in db.query(models.Outbox).filter_by(orden_id=order.id).order_by(models.Outbox.id).limit(200):
            details = item.datos if isinstance(item.datos, dict) else {}
            cid = details.get("correlation_id")
            if cid and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cid)):
                correlations.add(cid)
            transitions.setdefault(item.tipo, item)
            add(item.creado_en, "Saga: " + item.tipo, "outbox", details.get("estado", item.tipo), details.get("instancia"),
                "Notificación publicada" if item.publicado_en else "Notificación pendiente de publicar", None, f"Outbox de Compras #{item.id}", causes.get(item.tipo))
        payment = db.get(models.Pago, order.id)
        if payment:
            add(payment.creado_en, "Pago simulado", "pagos_simulados", payment.estado, detail="Estado actual del registro; fecha de creación del pago.")
        tickets = db.query(models.Entrada.estado, func.count()).filter_by(orden_id=order.id).group_by(models.Entrada.estado).all()
        for state, count in tickets:
            add(None, "Entradas y QR", "entradas", state, detail=f"{count} entradas; no se expone su código QR. Sin timestamp de emisión individual.")
    elif purchase:
        operation_ids = [purchase.operacion_id] if purchase.operacion_id else []
        purchases = [purchase]
    elif operation:
        operation_ids = [operation.operacion_id]
    for p in purchases:
        add(p.creado_en, "Compra persistida", "compras", p.estado, p.instancia, f"Compra {p.id}, evento {p.evento_id}, cantidad {p.cantidad}")
    claims = db.query(models.IdempotencyRequest).filter(models.IdempotencyRequest.operacion_id.in_(operation_ids)).all() if operation_ids else []
    for claim in claims:
        add(claim.created_at, "Solicitud idempotente", "purchase_idempotency", claim.state,
            detail="Existe respuesta persistida" if claim.response_status else "Respuesta final aún no persistida")
    # Consultar solo filas correlacionadas, nunca devolver el journal completo ni errores SQL.
    predicates = [models.RegistroJournal.detalles["operacion_id"].as_string().in_(operation_ids)] if operation_ids else []
    if purchases:
        predicates.append(models.RegistroJournal.detalles["compra_id"].as_integer().in_([p.id for p in purchases]))
    if order:
        predicates.append(models.RegistroJournal.detalles["orden_id"].as_string() == order.id)
    if predicates:
        for row in db.query(models.RegistroJournal).filter(or_(*predicates)).order_by(models.RegistroJournal.id).limit(200):
            details = row.detalles if isinstance(row.detalles, dict) else {}
            sent, received = details.get("reloj_enviado"), details.get("reloj_eventos")
            add(row.timestamp, row.tipo, "journal_compras", "registrado", row.instancia, "Evento funcional persistido; detalles sensibles omitidos.", row.reloj_t,
                f"Journal de Compras #{row.id}", f"Compras envió T={sent} y recibió T={received} de Eventos antes de este evento" if isinstance(sent, int) and isinstance(received, int) else None)
            cid = (row.detalles or {}).get("correlation_id")
            if cid and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cid)):
                correlations.add(cid)
    missing = ["No existe comprobante durable de entrega de la respuesta al navegador.",
        "No se persiste cada lectura previa de disponibilidad ni cada intento HTTP; una reserva confirma el efecto, no todos los intentos."]
    entries, _ = topology(events_url)
    events_target = next((e["url"] for e in entries if e["id"] == "eventos_entrada"), events_url)
    remote_states = {}
    for opid in operation_ids[:20]:
        result = fetch(events_target, "/eventos/reservas/" + quote(str(opid), safe=""), internal=True)
        data = result["datos"]
        remote_states[opid] = data["estado"] if result["ok"] and data.get("estado") else "ausente" if result["status"] == 404 else None
        if result["ok"] and data.get("estado"):
            # La instancia del encabezado respondió a esta lectura; no necesariamente registró la reserva.
            add(data.get("creado_en"), "Reserva de inventario", "reservas_stock (Eventos)", data["estado"], None,
                "Estado consultado ahora" + (f" a la instancia {clean(result.get('instancia'), 80)}" if result.get("instancia") else "")
                + "; no implica conocer la hora de cada transición ni quién la registró.")
        else:
            missing.append("Reserva remota no verificable para operación " + clean(opid, 80))
    if not correlations:
        missing.append("Registros históricos sin correlación HTTP persistida; se vinculan por orden/operación/compra.")
    timeline.sort(key=lambda e: (e["momento"] is None, e["momento"] or ""))
    return {"identificador": identifier, "orden_id": order.id if order else None,
        "estado": order.estado if order else purchase.estado if purchase else operation.state,
        "consultado_en": now().isoformat(), "correlaciones": sorted(correlations), "operaciones": operation_ids,
        "eventos": timeline, "faltantes": missing,
        "saga": saga_order_view(order, transitions, remote_states, payment) if order else saga_purchase_view(purchases, claims, remote_states),
        "idempotencia": ([{"referencia": "Pedido " + order.id, "clave": masked_key(order.clave), "estado": order.estado, "respuesta_http": None,
                "creado_en": iso_moment(order.creado_en), "intentos": None}] if order else []) + [{"referencia": "Operación " + claim.operacion_id, "clave": masked_key(claim.idempotency_key),
                "estado": claim.state, "respuesta_http": claim.response_status, "creado_en": iso_moment(claim.created_at), "intentos": None} for claim in claims[:20]],
        "limitaciones": ["Lecturas distribuidas sin snapshot global; estados pueden avanzar durante la consulta.",
            "Fechas de creación y eventos durables se distinguen de estados consultados sin timestamp.",
            "La trazabilidad no expone usuarios, claves idempotentes, tokens, cuerpos HTTP ni códigos QR."]}

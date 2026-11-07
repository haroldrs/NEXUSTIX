"""Observación administrativa acotada. No ejecuta acciones de infraestructura.

Los GET no escriben: solo las capturas/importaciones explícitas crean evidencia.
Las URL proceden exclusivamente de configuración del servidor, nunca del cliente.
"""
import json
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
from shared.operations_models import EvidenciaOperacion, IncidenteOperacion, ObservacionComponente

STATES = Literal["saludable", "degradado", "caido", "desconocido"]
SCENARIO = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
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


class CaptureRequest(BaseModel):
    model_config = {"extra": "forbid"}
    escenario: SCENARIO
    fase: Literal["inicial", "final", "observacion"] = "observacion"
    referencia_inicial: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
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
    ]
    result = [{"id": r[0], "nombre": r[1], "objetivo": r[2], "conceptos": r[3], "componentes": r[4],
             "requisitos": r[5], "accion": r[6], "esperado": r[7], "limitaciones": [r[8]]} for r in rows]
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


def fetch(url, path, authorization=None, internal=False):
    """GET fijo, sin redirecciones, proxies ambientales ni cuerpos no acotados."""
    if not url:
        return {"ok": False, "status": None, "datos": {}, "latencia_ms": None, "motivo": "Destino no configurado"}
    start = time.monotonic()
    try:
        headers = {"Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        if internal:
            headers.update(service_auth_headers())
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url + path, headers=headers, timeout=(1, 2), allow_redirects=False, stream=True) as response:
                body = bytearray()
                for chunk in response.iter_content(8192):
                    body.extend(chunk)
                    if len(body) > 131072:
                        raise ValueError()
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
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
        return fetch(url, path, credentials)

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
    diagnostic = metrics = None
    if url and entry["tipo"] == "api":
        diagnostic = read("/diagnostico/db", authorization)
        metrics = read("/diagnostico/metricas", authorization)
    return component, diagnostic, metrics


DIAGNOSTIC_KEYS = {"disponible", "rol", "comprobado_en", "version", "en_recuperacion", "replicas", "receptor_wal", "archivado",
    "wal", "checkpoints", "ultimo_checkpoint", "conexiones", "durabilidad", "ultimo_failover", "consultas_no_disponibles", "estado", "alcance", "limitaciones",
    "application_name", "state", "sync_state", "sent_lsn", "write_lsn", "flush_lsn", "replay_lsn", "lag_bytes", "replay_lag_seconds",
    "status", "written_lsn", "flushed_lsn", "latest_end_lsn", "last_msg_receipt_time", "recibido_lsn", "aplicado_lsn", "ultimo_replay", "replay_pausado",
    "actual_lsn", "persistido_lsn", "archived_count", "failed_count", "last_archived_time", "last_failed_time", "num_timed", "num_requested", "write_time", "sync_time",
    "buffers_written", "stats_reset", "checkpoint_lsn", "redo_lsn", "timeline_id", "checkpoint_time", "total", "activas", "maximas", "fsync", "full_page_writes", "synchronous_commit", "wal_level"}
METRIC_KEYS = {"disponible", "servicio", "instancia", "desde", "alcance", "solicitudes", "exitosas", "errores_http", "latencia_media_ms", "latencia_max_ms", "recientes", "momento", "correlation_id", "metodo", "ruta", "status", "latencia_ms"}


def whitelist(value, keys, depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {k: whitelist(v, keys, depth + 1) for k, v in value.items() if k in keys}
    if isinstance(value, list):
        return [whitelist(v, keys, depth + 1) for v in value[:100]]
    return clean(value) if isinstance(value, str) else value if isinstance(value, (bool, int, float)) else None


def node_snapshot(events_url, authorization):
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
    if time.monotonic() < deadline:
        result = fetch(events_url, "/inventario/estado", authorization)
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
            "Lecturas tomadas en momentos distintos; solo capturas explícitas conservan incidentes y evidencias."]}


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
        "conceptos": scenario["conceptos"], "componentes": scenario["componentes"],
        "acciones": clean(data.acciones), "esperado": scenario["esperado"], "observado": clean(data.observado, 2000),
        "evidencias": observation, "limitaciones": scenario["limitaciones"] + observation.get("limitaciones", []),
        "conclusion": clean(data.conclusion), "resultado": "sin_evaluar", "origen": "captura",
        "fase": getattr(data, "fase", "observacion"), "referencia_inicial": getattr(data, "referencia_inicial", None),
        "ventana_observacion_segundos": None}


def install(app, models, get_db, require_admin, events_url):
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
        report = report_document(data, snapshot(events_url, authorization))
        report["evidencias"]["pendientes"] = pending_values(db, models)
        if initial:
            report["estado_inicial"] = initial.documento["evidencias"]
            report["ventana_observacion_segundos"] = round((datetime.fromisoformat(report["fecha"]) - datetime.fromisoformat(initial.documento["fecha"])).total_seconds(), 2)
        db.add(EvidenciaOperacion(id=report["id"], escenario=data.escenario, documento=report))
        transitions(db, report)
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

    def add(moment, stage, source, state, instance=None, detail=""):
        timeline.append({"momento": moment.isoformat() if isinstance(moment, datetime) else moment,
            "etapa": clean(stage, 100), "fuente": source, "estado": clean(state, 80),
            "instancia": clean(instance, 80) or None, "detalle": clean(detail, 600)})

    operation_ids, purchases = [], []
    if order:
        add(order.creado_en, "Solicitud persistida", "ordenes", order.estado, detail="Estado actual de la orden; la fecha corresponde a su creación.")
        operation_ids = [item["operacion_id"] for item in order.items if isinstance(item, dict) and item.get("operacion_id")]
        purchases = db.query(models.Compra).filter(models.Compra.operacion_id.in_(operation_ids)).all() if operation_ids else []
        for item in db.query(models.Outbox).filter_by(orden_id=order.id).order_by(models.Outbox.id).limit(200):
            details = item.datos if isinstance(item.datos, dict) else {}
            cid = details.get("correlation_id")
            if cid and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cid)):
                correlations.add(cid)
            add(item.creado_en, "Saga: " + item.tipo, "outbox", details.get("estado", item.tipo), details.get("instancia"),
                "Notificación publicada" if item.publicado_en else "Notificación pendiente de publicar")
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
            add(row.timestamp, row.tipo, "journal_compras", "registrado", row.instancia, "Evento funcional persistido; detalles sensibles omitidos.")
            cid = (row.detalles or {}).get("correlation_id")
            if cid and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cid)):
                correlations.add(cid)
    missing = ["No existe comprobante durable de entrega de la respuesta al navegador.",
        "No se persiste cada lectura previa de disponibilidad ni cada intento HTTP; una reserva confirma el efecto, no todos los intentos."]
    entries, _ = topology(events_url)
    events_target = next((e["url"] for e in entries if e["id"] == "eventos_entrada"), events_url)
    for opid in operation_ids[:20]:
        result = fetch(events_target, "/eventos/reservas/" + quote(str(opid), safe=""), internal=True)
        data = result["datos"]
        if result["ok"] and data.get("estado"):
            add(data.get("creado_en"), "Reserva de inventario", "reservas_stock (Eventos)", data["estado"],
                result.get("instancia"), "Estado consultado ahora; no implica conocer la hora de cada transición.")
        else:
            missing.append("Reserva remota no verificable para operación " + clean(opid, 80))
    if not correlations:
        missing.append("Registros históricos sin correlación HTTP persistida; se vinculan por orden/operación/compra.")
    timeline.sort(key=lambda e: (e["momento"] is None, e["momento"] or ""))
    return {"identificador": identifier, "orden_id": order.id if order else None,
        "estado": order.estado if order else purchase.estado if purchase else operation.state,
        "consultado_en": now().isoformat(), "correlaciones": sorted(correlations), "operaciones": operation_ids,
        "eventos": timeline, "faltantes": missing,
        "limitaciones": ["Lecturas distribuidas sin snapshot global; estados pueden avanzar durante la consulta.",
            "Fechas de creación y eventos durables se distinguen de estados consultados sin timestamp.",
            "La trazabilidad no expone usuarios, claves idempotentes, tokens, cuerpos HTTP ni códigos QR."]}

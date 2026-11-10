"""Centro de Control: observación HTTP redundante, sin dependencia SQL local.

No hay un daemon de infraestructura ni acciones sobre Docker. Cada réplica API
mantiene una caché/historial acotados en memoria; sólo el endpoint existente de
captura de Compras persiste evidencia cuando el administrador lo solicita.
"""
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from threading import Lock, RLock
import time
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from shared import node_operations
from shared.auth_tokens import authenticated_claims
from shared.node_admission import AnnouncementError, MAX_BYTES, registry

CACHE_SECONDS = 25
HTTP_KINDS = {"api", "frontend", "gateway", "ruta"}


def require_observer_admin(authorization: str | None = Header(default=None)):
    """Sólo observación: verifica firma/rol/expiración sin consultar Usuarios.

    Una revocación de cuenta se refleja aquí al expirar el access token, como en
    las otras API verificadoras. No sustituye la autorización de negocio.
    """
    claims = authenticated_claims(authorization)
    if claims["role"] != "administrador":
        raise HTTPException(403, "Se requiere un administrador")
    return claims


def instant():
    return datetime.now(timezone.utc).isoformat()


def age(timestamp):
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            return None
        return max(0, round(time.time() - parsed.timestamp(), 1))
    except (ValueError, TypeError):
        return None


def coordination_observations():
    """Lectura opcional de Patroni: destinos fijados en el servidor, nunca por el navegador."""
    try:
        endpoints = json.loads(os.getenv("NEXUSTIX_COORDINATION_ENDPOINTS") or "{}")
    except ValueError:
        return []
    if not isinstance(endpoints, dict):
        return []
    import requests
    from urllib.parse import urlsplit
    rows = []
    for base, url in list(endpoints.items())[:8]:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            continue
        row = {"base": str(base)[:40], "estado": "desconocido", "miembros": []}
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(url.rstrip("/") + "/cluster", timeout=(1, 2), allow_redirects=False)
            members = response.json().get("members") if response.status_code == 200 else None
            if isinstance(members, list):
                row["miembros"] = [{key: member.get(key) for key in ("name", "role", "state", "timeline") if isinstance(member.get(key), (str, int))}
                    for member in members[:16] if isinstance(member, dict)]
                row["estado"] = "observado"
        except (requests.RequestException, ValueError, AttributeError):
            pass
        rows.append(row)
    return rows


def coordinator(document=None):
    """Evalúa preparación. Ninguna rama autoriza promociones ni declara HA operativa."""
    nodes = document["nodes"] if document else []
    domains = {node["id"]: node.get("failure_domain") for node in nodes}
    plan = document.get("coordination_plan") if document else None
    voters = [voter for voter in plan["voters"] if voter in domains] if plan else []
    majority = len(voters) // 2 + 1 if voters else None
    worst = max(sum(domains[voter] == domain for voter in voters) for domain in {domains[voter] for voter in voters}) if voters else None
    survives = len(voters) - worst >= majority if voters else None
    observed = coordination_observations() if document else []
    seen = any(row["estado"] == "observado" for row in observed)
    databases = document["databases"].items() if document else []
    standbys_ok = bool(databases) and all(any(domains.get(node) != domains.get(db["primary"]) for node in db["standbys"]) for _, db in databases)
    gateways = {node.get("failure_domain") for node in nodes if "gateway" in node.get("roles", [])}
    access = (document or {}).get("access", {})

    def need(name, state, detail):
        return {"requisito": name, "estado": state, "detalle": detail}

    requirements = [
        need("Tres dominios físicos independientes", "cumplido" if len(set(domains.values())) >= 3 else "pendiente", f"{len(set(domains.values()))} declarados en el inventario."),
        need("Votantes de consenso declarados", "cumplido" if voters else "no_configurado", f"{len(voters)} votantes en el plan." if voters else "Sin plan de coordinación en el inventario."),
        need("Mayoría tras perder un dominio", "no_configurado" if not voters else "cumplido" if survives else "pendiente", "Cálculo sobre la declaración; no acredita conectividad."),
        need("Coordinador desplegado y observado", "cumplido" if seen else "no_configurado", "Respuesta real de Patroni, sin quórum de etcd verificado." if seen else "Coordinador no instalado."),
        need("Fencing verificable del escritor anterior", "pendiente", "Sin mecanismo independiente comprobado. Bloquea cualquier promoción automática."),
        need("Standby en otro dominio para cada base", "cumplido" if standbys_ok else "pendiente", "Según el inventario; no garantiza que conserve todos los commits."),
        need("Gateways en dos o más dominios", "cumplido" if len(gateways) >= 2 else "pendiente", f"{len(gateways)} dominios con gateway declarados."),
        need("Acceso estable verificado", "no_verificable" if access.get("mode") not in {None, "manual"} else "no_configurado", "La infraestructura externa no se comprueba desde aquí." if access.get("mode") not in {None, "manual"} else "Acceso manual por direcciones alternativas."),
        need("Failover y reincorporación ensayados", "no_verificable", "Sin evidencia de un ensayo registrado."),
    ]
    if not document:
        requirements = []  # Sin inventario no hay nada que evaluar.
    return {"configurado": seen, "estado": "observado" if seen else "preparado_inactivo" if plan else "no_configurado", "quorum_observado": None,
        "promocion_automatica": False, "failover_bloqueado": True, "ha_operativa": False,
        "mensaje": None if plan or seen else "Coordinador no instalado. Failover PostgreSQL automático no disponible.",
        "votantes_declarados": [{"nodo": voter, "dominio": domains[voter]} for voter in voters], "mayoria_requerida": majority,
        "conserva_mayoria_tras_perder_dominio": survives, "observaciones": observed, "requisitos": requirements,
        "motivos": ["No hay coordinador de consenso operativo ni identidad de sus votantes verificada." if not seen else "Observar a Patroni no autoriza promociones desde NEXUSTIX.",
            "No se ha integrado ni comprobado fencing independiente para las computadoras declaradas.",
            "El quórum WAL no elige un líder. Conservar la recuperación manual coordinada."]}


class ClusterObserver:
    def __init__(self):
        self.id = uuid4().hex
        self.started = instant()
        self.lock = RLock()
        self.collecting = Lock()
        self.history = deque(maxlen=256)
        self.states = OrderedDict()
        self.last_nodes = OrderedDict()
        self.component_memory = OrderedDict()
        self.cached = None
        self.cached_at = 0.0
        self.cached_key = None
        self.deployment = None

    def identity(self):
        def field(name):
            value = os.getenv(name, "")
            return value[:96] if value and all(char.isalnum() or char in "_-.:+" for char in value) else None
        return {"id": self.id, "instancia": field("INSTANCE_ID"), "servicio": field("NEXUSTIX_SERVICE"),
            "nodo": field("NEXUSTIX_NODE_ID"), "iniciado_en": self.started, "alcance": "memoria_por_proceso",
            "historial_persistente": False, "cache_segundos": CACHE_SECONDS,
            "disponibilidad_persistencia_independiente": True}

    def event(self, kind, component, previous, current, description, node=None, origin="sondeo_http"):
        event = {"id": self.id + ":" + uuid4().hex, "fecha": instant(), "tipo": kind, "componente": component,
            "nodo": node, "estado_anterior": previous, "estado_actual": current, "origen": origin,
            "descripcion": description, "failover_confirmado": False}
        self.history.append(event)
        return event

    def transition(self, key, value, kind, description, *, node=None, origin="sondeo_http", initial=False):
        previous = self.states.get(key)
        if previous != value and (previous is not None or initial):
            self.event(kind, key, previous, value, description, node, origin)
        self.states[key] = value
        self.states.move_to_end(key)
        while len(self.states) > 1024:
            self.states.popitem(last=False)

    @staticmethod
    def scope_key(document):
        return (document["deployment"], hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest())

    def unavailable(self, warnings):
        return {"capturado_en": instant(), "componentes": [], "persistencia": [], "metricas": [],
            "infraestructura": {"modo": "inventario_invalido" if node_operations.enabled() else "sin_inventario"},
            "cluster": {"estado": "desconocido", "resumen": {}, "nodos": [], "bases": [],
                "coordinador": coordinator(), "acceso": {}, "descubrimiento": {"habilitado": False, "candidatos": []},
                "eventos": [], "observador": self.identity(), "limitaciones": warnings or [
                    "Este entorno no tiene un inventario multinodo activo. Sus servicios anteriores se mantienen."]}}

    def view(self, snapshot, *, cached=False, collecting=False):
        result = deepcopy(snapshot)
        cluster = result["cluster"]
        cluster["observador"].update({"desde_cache": cached, "observacion_en_curso": collecting,
            "antiguedad_segundos": age(result["capturado_en"])})
        for candidate in cluster.get("descubrimiento", {}).get("candidatos", []):
            if datetime.fromisoformat(candidate["expira_en"]).timestamp() <= time.time():
                candidate.update({"estado": "expirado", "identidad_validada": False})
        for node in cluster["nodos"]:
            node["segundos_desde_respuesta"] = age(node.get("ultima_respuesta"))
            node["admision"]["identidad_validada"] = any(candidate["node_id"] == node["id"]
                and candidate["identidad_validada"] for candidate in cluster["descubrimiento"]["candidatos"])
        # Una muestra demasiado antigua nunca vuelve a presentarse como actual.
        if (age(result["capturado_en"]) or 0) > 90:
            cluster["estado"] = "desconocido"
            for node in cluster["nodos"]:
                node["estado"] = "desconocido"
            cluster["limitaciones"].append("La última captura tiene más de 90 segundos; se conserva como antecedente.")
        return result

    def read(self, authorization):
        document, warnings = node_operations.load_inventory()
        if not document:
            return self.unavailable(warnings)
        key = self.scope_key(document)
        with self.lock:
            if self.cached and self.cached_key == key and time.monotonic() - self.cached_at < CACHE_SECONDS:
                return self.view(self.cached, cached=True)
        if not self.collecting.acquire(blocking=False):
            with self.lock:
                if self.cached and self.cached_key == key:
                    return self.view(self.cached, cached=True, collecting=True)
                result = self.unavailable(["Hay una observación en curso en esta réplica. La siguiente consulta recogerá su resultado."])
                result["cluster"]["observador"]["observacion_en_curso"] = True
                return result
        try:
            # Importación diferida: no registra modelos de Compras en Usuarios/Eventos.
            from shared.operations import node_snapshot
            result = node_snapshot(os.getenv("EVENTOS_URL", "http://router:8002"), authorization,
                                   include_business_inventory=False)
            return self.record(result, document)
        finally:
            self.collecting.release()

    def record(self, snapshot, document=None):
        if document is None:
            document, _ = node_operations.load_inventory()
        if not document or snapshot.get("infraestructura", {}).get("modo") != "inventario_nodos":
            return self.unavailable(["No hay una observación asociable al inventario actual."])
        if (snapshot["infraestructura"].get("despliegue") != document["deployment"]
                or snapshot["infraestructura"].get("inventory_sha256") != document.get("inventory_sha256")):
            return self.unavailable(["El inventario cambió durante la captura; vuelva a consultar para no mezclar topologías."])
        with self.lock:
            if (self.cached and self.cached_key == self.scope_key(document)
                    and snapshot["capturado_en"] < self.cached["capturado_en"]):
                # Una captura explícita puede terminar después de otra lectura.
                # No retroceder el historial ni contabilizar otra vez sus fallos.
                return self.view(self.cached, cached=True)
            if self.deployment != document["deployment"]:
                self.history.clear()
                self.states.clear()
                self.last_nodes.clear()
                self.deployment = document["deployment"]
            moment = snapshot["capturado_en"]
            components = snapshot["componentes"]
            infrastructure = snapshot["infraestructura"]
            candidates = registry.list(document)
            node_rows = []
            for configured in document["nodes"]:
                node_id = configured["id"]
                node = next(row for row in infrastructure["nodos"] if row["id"] == node_id)
                probes = [row for row in components if row.get("nodo") == node_id and row.get("tipo") in HTTP_KINDS]
                answers = [row for row in probes if row.get("http_status") is not None or row.get("respuesta_administrativa_en")]
                attempted = [row for row in probes if row.get("sonda_realizada") is True]
                fully_attempted = bool(probes) and len(attempted) == len(probes)
                previous = self.last_nodes.get(node_id, {})
                misses = 0 if answers else previous.get("fallos_consecutivos", 0) + 1 if fully_attempted else previous.get("fallos_consecutivos", 0)
                identities = [row["identidad_observada"] for row in probes if isinstance(row.get("identidad_observada"), dict)]
                versions = sorted({identity["release"] for identity in identities if identity.get("release")})
                drift = any(identity.get("node_id") != node_id or identity.get("deployment_id") != document["deployment"]
                    or (document.get("inventory_sha256") and identity.get("inventory_sha256") != document["inventory_sha256"])
                    for identity in identities)
                healthy = bool(probes) and all(row.get("estado") == "saludable" for row in probes) and not drift
                state = "activo" if healthy else "degradado" if answers else "no_disponible" if fully_attempted and misses >= 2 and previous.get("ultima_respuesta") else "desconocido"
                candidate = next((row for row in candidates if row["node_id"] == node_id and row["identidad_validada"]), None)
                latest_response = max((row.get("respuesta_administrativa_en") or row.get("comprobado_en") or moment for row in answers), default=previous.get("ultima_respuesta"))
                last_state = state if answers else previous.get("ultimo_estado_conocido", "desconocido")
                memory = {"ultima_respuesta": latest_response, "ultimo_estado_conocido": last_state,
                    "fallos_consecutivos": misses, "ultima_observacion": moment if attempted else previous.get("ultima_observacion")}
                self.last_nodes[node_id] = memory
                self.last_nodes.move_to_end(node_id)
                self.transition("configuracion:" + node_id, ",".join(sorted(node["roles"])), "nodo_incorporado_configuracion",
                    "Asignación declarada en el inventario generado; no implica activación ni identidad física comprobada.",
                    node=node_id, origin="inventario", initial=True)
                if attempted or answers:
                    self.transition("nodo:" + node_id, state, "nodo_vuelve_a_responder" if answers else "nodo_sin_respuesta",
                        "Respuestas HTTP observadas desde esta réplica." if answers else "Sin respuesta completa desde este observador; no confirma apagado físico.", node=node_id)
                self.transition("version:" + node_id, "discordante" if drift else "coincidente" if identities else "desconocida",
                    "configuracion_observada", "Comparación de metadatos reportados por las API y el inventario; no verifica el contenido de la imagen.", node=node_id)
                node_rows.append({**node, **memory, "estado": state, "respondio": bool(answers),
                    "respuestas_http": len(answers),
                    "admision": {"configurado": True, "detectado": bool(answers or latest_response or candidate),
                        "identidad_validada": candidate is not None, "autorizado": bool(node.get("identity_fingerprint")),
                        "incorporado": True, "activacion_verificada": False},
                    "segundos_desde_respuesta": age(latest_response), "componentes": probes,
                    "servicios_responden": [row["id"] for row in answers],
                    "metricas_host": None, "version_despliegue": ", ".join(versions) if versions else None,
                    "version_declarada": document.get("release"), "configuracion_discordante": drift,
                    "ultimo_heartbeat": None,
                    "alcance_identidad": "Incorporado significa presente en el manifiesto confiable; HTTP y etiquetas de versión no sustituyen identidad firmada ni autorización de activación."})
            while len(self.last_nodes) > 64:
                self.last_nodes.popitem(last=False)
            for component in components:
                if component.get("tipo") in HTTP_KINDS and component.get("sonda_realizada"):
                    state = component.get("estado", "desconocido")
                    # Memoria por observador: permite distinguir un fallo aislado de uno repetido.
                    memory = self.component_memory.setdefault(component["id"], {"fallos": 0, "ultima_valida": None})
                    self.component_memory.move_to_end(component["id"])
                    memory["fallos"] = 0 if state == "saludable" else memory["fallos"] + 1
                    if state == "saludable":
                        memory["ultima_valida"] = component.get("comprobado_en")
                    component["fallos_consecutivos"] = memory["fallos"]
                    component["ultima_respuesta_valida"] = memory["ultima_valida"]
                    while len(self.component_memory) > 256:
                        self.component_memory.popitem(last=False)
                    self.transition("servicio:" + component["id"], state,
                        "servicio_recuperado" if state == "saludable" else "servicio_no_disponible" if state == "degradado" else "servicio_no_verificable",
                        component.get("detalle", "Observación HTTP"), node=component.get("nodo"))
            for database in infrastructure["bases"]:
                primary = database.get("primario_observado")
                if primary:
                    self.transition("primario:" + database["base"], primary["identidad_nodo"], "cambio_primario_observado",
                        "Cambió la identidad SQL del primario consultado; no certifica failover seguro ni fencing.",
                        node=primary.get("nodo"), origin="diagnostico_sql")
                if database.get("posible_conflicto_primarios"):
                    self.transition("conflicto:" + database["base"], "conflicto", "failover_bloqueado",
                        "Las API informan identidades primarias distintas. No promover; requiere revisión y aislamiento coordinado.", origin="diagnostico_sql", initial=True)
                else:
                    self.states.pop("conflicto:" + database["base"], None)
                quorum = database.get("quorum_wal", {})
                self.transition("quorum_wal:" + database["base"], quorum.get("estado", "desconocido"), "cambio_quorum_wal",
                    "Estado de confirmaciones WAL observado; no se observa ni se infiere consenso de elección.", origin="diagnostico_sql")
                for standby in database["standbys"]:
                    state = standby.get("estado_observacion", "desconocido")
                    self.transition("standby:" + database["base"] + ":" + standby["nodo"], state,
                        "standby_recuperado" if state == "activo" else "standby_no_disponible" if state == "no_disponible" else "standby_no_verificable",
                        "Canal WAL consultado desde el primario; la ausencia de una medición no acredita pérdida física.",
                        node=standby["nodo"], origin="diagnostico_sql")
            summary, state = self.summary(node_rows, components, infrastructure["bases"])
            self.transition("cluster", state, "recuperacion_observada" if state == "operativo" else "cluster_degradado" if state == "degradado" else "cluster_no_verificable",
                "Cambio del estado agregado de esta observación; no constituye una validación de alta disponibilidad.")
            self.transition("failover_automatico", "bloqueado", "failover_bloqueado",
                "Faltan consenso y fencing integrado comprobable. La recuperación PostgreSQL sigue siendo manual.", origin="configuracion", initial=True)
            events = list(reversed(self.history))
            for node in node_rows:
                node["eventos"] = [event for event in events if event["nodo"] == node["id"]][:12]
            access = document.get("access", {})
            stable = f"{access.get('scheme', 'http')}://{access['hostname']}:{document['ports']['public']}" if access.get("hostname") and access.get("mode") != "manual" else None
            cluster = {"estado": state, "resumen": summary, "nodos": node_rows, "bases": infrastructure["bases"],
                "coordinador": coordinator(document), "acceso": {"modo": access.get("mode", "manual"), "hostname": access.get("hostname"),
                    "esquema": access.get("scheme", "http"), "dominios_infraestructura": access.get("failure_domains", []),
                    "url_estable": stable, "verificado": False, "alternativas": document["access_points"]},
                "descubrimiento": {"habilitado": True, "modo": "anuncios_firmados", "candidatos": candidates,
                    "pasos": ["Anuncio firmado", "Verificar huella por otro canal", "Autorizar pin en inventario",
                        "Validar recursos y compatibilidad", "Regenerar y distribuir", "Activación operativa por el responsable"],
                    "alcance": "Anuncios importados por administrador; registro en memoria por observador. No hay escaneo LAN, alta automática ni ejecución sobre candidatos."},
                "eventos": events, "observador": self.identity(),
                "limitaciones": ["Estado agregado de lecturas HTTP y SQL; no acredita commits de negocio ni alta disponibilidad física.",
                    "Historial acotado a 256 eventos en memoria de esta réplica. Reinicios pierden ese historial; una captura explícita del laboratorio conserva evidencia en Compras.",
                    "El observador puede atender con un JWT de administrador vigente sin su DB local, mientras el proceso ya esté ejecutándose. Arranque, login y renovación de sesión pueden depender de PostgreSQL.",
                    "Nodos sin sonda HTTP directa, respuestas omitidas y métricas de host ausentes permanecen desconocidos. No disponible significa falta de respuesta desde este observador, no PC apagada.",
                    "Metadatos de versión y cluster_name son declaraciones reportadas por procesos; no prueban imagen ejecutada ni independencia física.",
                    "La autorización de candidatos se registra fuera del portal, en el inventario. El anuncio no modifica roles, primarios ni miembros de consenso.",
                    *snapshot.get("limitaciones", [])]}
            result = {**snapshot, "cluster": cluster}
            self.cached, self.cached_at, self.cached_key = deepcopy(result), time.monotonic(), self.scope_key(document)
            return self.view(result)

    @staticmethod
    def summary(nodes, components, databases):
        replicas = []
        failed_service = False
        missing_service = False
        for service in ("usuarios", "eventos", "compras", "frontend", "gateway"):
            rows = [row for row in components if row.get("servicio") == service and row.get("tipo") != "ruta" and row.get("tipo") in HTTP_KINDS]
            active = sum(row.get("estado") == "saludable" for row in rows)
            replicas.append({"servicio": service, "configuradas": len(rows), "activas": active})
            if not active:
                missing_service = True
                failed_service |= bool(rows) and all(row.get("http_status") is not None and row.get("estado") == "degradado" for row in rows)
        evidence = any(node["respondio"] for node in nodes)
        all_healthy = bool(nodes) and all(node["estado"] == "activo" for node in nodes)
        db_complete = len(databases) == 3 and all(database.get("primario_observado")
            and database["primario_observado"].get("nodo") == database["primario_configurado"]
            and not database.get("discordancia_rol") and database.get("roles_observados") == ["primario"]
            and database.get("quorum_wal", {}).get("estado") in {"disponible", "no_requerido"}
            and all(standby.get("estado_observacion") == "activo" for standby in database["standbys"])
            for database in databases)
        state = "no_disponible" if failed_service else "desconocido" if not evidence else "operativo" if all_healthy and db_complete and not missing_service else "degradado"
        return {"nodos_configurados": len(nodes), "nodos_responden": sum(node["respondio"] for node in nodes),
            "nodos_no_disponibles": sum(node["estado"] == "no_disponible" for node in nodes),
            "nodos_desconocidos": sum(node["estado"] == "desconocido" for node in nodes), "replicas": replicas,
            "gateways_disponibles": next(row["activas"] for row in replicas if row["servicio"] == "gateway")}, state


observer = ClusterObserver()


def install(app):
    @app.get("/health/observador", include_in_schema=False)
    def observer_health():
        # Sólo liveness de proceso para el pool administrativo; no declara
        # disponibilidad de la base, del negocio ni del clúster.
        return {"status": "ok", "alcance": "proceso_observador"}

    @app.get("/admin/operaciones/cluster")
    def cluster_state(authorization: str | None = Header(default=None), _admin=Depends(require_observer_admin)):
        return JSONResponse(observer.read(authorization), headers={"Cache-Control": "no-store"})

    @app.get("/admin/operaciones/cluster/nodos/{node_id}")
    def node_detail(node_id: str, authorization: str | None = Header(default=None), _admin=Depends(require_observer_admin)):
        snapshot = observer.read(authorization)
        node = next((node for node in snapshot["cluster"]["nodos"] if node["id"] == node_id), None)
        if node is None:
            raise HTTPException(404, "Nodo no presente en el inventario observado")
        return JSONResponse({"nodo": node, "capturado_en": snapshot["capturado_en"], "observador": observer.identity()}, headers={"Cache-Control": "no-store"})

    @app.get("/admin/operaciones/cluster/eventos")
    def cluster_events(limit: int = Query(default=100, ge=1, le=256), _admin=Depends(require_observer_admin)):
        with observer.lock:
            events = list(reversed(observer.history))[:limit]
        return JSONResponse({"eventos": events, "observador": observer.identity()}, headers={"Cache-Control": "no-store"})

    @app.post("/admin/operaciones/cluster/anuncios")
    async def announcement(request: Request, _admin=Depends(require_observer_admin)):
        document, _ = node_operations.load_inventory()
        if not document:
            raise HTTPException(409, "Configure un inventario multinodo antes de recibir candidatos")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise HTTPException(413, "El anuncio excede 16 KiB")
        try:
            envelope = json.loads(raw)
            candidate, fresh = registry.ingest(envelope, document)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            message = str(error) if isinstance(error, AnnouncementError) else "Formato de anuncio inválido"
            raise HTTPException(422, message) from None
        with observer.lock:
            if fresh:
                observer.event("nodo_anunciado", "candidato:" + candidate["id"], None, candidate["estado"],
                    "Anuncio Ed25519 recibido para revisión; no se ha activado ni autorizado infraestructura.",
                    node=candidate["node_id"], origin="anuncio_firmado_importado")
            observer.cached_at = 0
        return JSONResponse({"candidato": candidate, "observador": observer.identity()}, status_code=201 if fresh else 200,
                            headers={"Cache-Control": "no-store"})

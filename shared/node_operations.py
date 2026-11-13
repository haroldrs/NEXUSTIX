"""Inventario de despliegue y proyecciones estáticas, sin acciones de infraestructura.

Solo se acepta el documento generado montado por el servidor. Ninguna dirección
de sonda se recibe del navegador. Las proyecciones no ejecutan fallos ni prueban HA.
"""
import ipaddress
import json
import os
import re
from pathlib import Path


DOMAINS = ("usuarios", "eventos", "compras")
ROLES = {"frontend", "gateway", *DOMAINS}
MAX_NODES = 32
DEFAULT_PORTS = {"public": 8080, "usuarios": 8001, "eventos": 8002, "compras": 8005,
    "frontend_backend": 8180, "usuarios_backend": 18001, "eventos_backend": 18002,
    "compras_backend": 18003, "usuarios_db": 5432, "eventos_db": 5433, "compras_db": 5434}


def enabled():
    return bool(os.getenv("NEXUSTIX_OPERATIONS_INVENTORY_FILE"))


def _label(value, maximum=80):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("Etiqueta de inventario inválida")
    return value


def _address(value):
    value = _label(value, 253)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
            raise ValueError("Dirección de inventario inválida") from None
    return value


def load_inventory():
    """Acota el archivo y conserva únicamente los campos públicos conocidos."""
    if not enabled():
        return None, []
    try:
        with Path(os.environ["NEXUSTIX_OPERATIONS_INVENTORY_FILE"]).open("rb") as source:
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError("Inventario demasiado grande")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Versión de inventario incompatible")
        raw_nodes = data["nodes"]
        if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= MAX_NODES:
            raise ValueError("Cantidad de nodos fuera de límite")
        nodes, identifiers = [], set()
        for node in raw_nodes:
            node_id = node["id"]
            if not isinstance(node_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", node_id) or node_id in identifiers:
                raise ValueError("Identificador de nodo inválido")
            identifiers.add(node_id)
            roles = node.get("roles", [])
            if not isinstance(roles, list) or any(role not in ROLES for role in roles) or len(set(roles)) != len(roles):
                raise ValueError("Roles inválidos")
            resources = node.get("resources", {})
            cpus, memory = resources.get("cpus"), resources.get("memory_mb")
            if (isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0 < cpus <= 4096
                    or isinstance(memory, bool) or not isinstance(memory, int) or not 1 <= memory <= 16777216):
                raise ValueError("Recursos inválidos")
            reserved = node.get("reserved_resources", {})
            reserved = {key: value for key, value in reserved.items() if key in {"cpus", "memory_mb"}
                and type(value) in {int, float} and 0 <= value <= 16777216} if isinstance(reserved, dict) else {}
            fingerprint = node.get("identity_fingerprint")
            if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)):
                raise ValueError("Huella de identidad inválida")
            nodes.append({"id": node_id, "address": _address(node["address"]),
                "platform": _label(node["platform"], 40), "failure_domain": _label(node["failure_domain"]),
                "resources": {"cpus": cpus, "memory_mb": memory}, "reserved_resources": reserved, "roles": roles,
                "identity_fingerprint": fingerprint})
        databases = {}
        for domain in DOMAINS:
            db = data["databases"][domain]
            primary, standbys = db["primary"], db["standbys"]
            if (primary not in identifiers or not isinstance(standbys, list) or primary in standbys
                    or len(set(standbys)) != len(standbys) or any(n not in identifiers for n in standbys)):
                raise ValueError("Asignación de PostgreSQL inválida")
            replication, quorum = db["replication"], db.get("sync_quorum", 0)
            if replication not in {"async", "sync"} or type(quorum) is not int or quorum < 0 or quorum > len(standbys):
                raise ValueError("Replicación inválida")
            if (replication == "sync" and quorum < 1) or (replication == "async" and quorum != 0):
                raise ValueError("Quórum incompatible")
            generations = {node_id: db["generations"][node_id] for node_id in [primary, *standbys]}
            if any(type(value) is not int or value < 1 for value in generations.values()):
                raise ValueError("Generaciones inválidas")
            databases[domain] = {"primary": primary, "standbys": standbys, "replication": replication,
                "sync_quorum": quorum, "generations": generations}
        ports = {key: data.get("ports", {}).get(key, default) for key, default in DEFAULT_PORTS.items()}
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports.values()):
            raise ValueError("Puerto inválido")
        preferred = data["gateway"]["preferred"]
        if preferred not in {n["id"] for n in nodes if "gateway" in n["roles"]}:
            raise ValueError("Gateway preferido inválido")
        document = {"version": 1, "deployment": _label(data["deployment"]), "profile": _label(data["profile"], 40),
            "nodes": nodes, "databases": databases, "ports": ports, "gateway": {"preferred": preferred},
            "automatic_failover": False, "physical_ha_verified": False,
            "ha_status": "prepared_manual_recovery" if data["profile"] == "alta_disponibilidad" else "not_claimed"}
        digest = data.get("inventory_sha256")
        document["inventory_sha256"] = digest if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) else None
        document["release"] = _label(data.get("release", "sin-etiqueta"), 96)
        access = data.get("access", {"mode": "manual"})
        if not isinstance(access, dict) or access.get("mode") not in {"manual", "external_dns", "external_proxy"}:
            raise ValueError("Acceso de inventario inválido")
        hostname = access.get("hostname")
        if hostname is not None and (not isinstance(hostname, str) or not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", hostname)):
            raise ValueError("Hostname de acceso inválido")
        document["access"] = {"mode": access["mode"], "hostname": hostname,
            "scheme": "https" if access.get("scheme") == "https" else "http",
            "failure_domains": [_label(item, 40) for item in access.get("failure_domains", [])][:32],
            "verified": False, "automatic_switch_verified": False}
        plan = data.get("coordination_plan")
        known = {n["id"] for n in nodes}
        if isinstance(plan, dict) and plan.get("mode") in {"prepared_inactive", "patroni"} and isinstance(plan.get("voters"), list):
            document["coordination_plan"] = {"mode": plan["mode"], "voters": [v for v in plan["voters"] if v in known][:5]}
        # Se reconstruyen las URL desde los destinos validados, nunca se reenvían
        # campos arbitrarios de access_points ni secretos del documento original.
        document["access_points"] = [{"node": n["id"], "url": base_url(n, ports["public"]),
            "preferred": n["id"] == preferred} for n in nodes if "gateway" in n["roles"]]
        return document, []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None, ["Inventario de nodos ausente o inválido. No se consultan destinos alternativos por convención; revisar el archivo configurado en el servidor."]


def component_id(role, node_id):
    return role + "_" + node_id.replace("-", "_")


def base_url(node, port):
    host = node["address"]
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


def component_entries(document):
    entries = []
    for node in document["nodes"]:
        local = node["id"] == os.getenv("NEXUSTIX_NODE_ID")
        for role in node["roles"]:
            port = document["ports"]["public" if role == "gateway" else role + "_backend"]
            internal_port = {"usuarios": 8001, "eventos": 8002, "compras": 8003, "frontend": 80}.get(role, port)
            entry = {"id": component_id(role, node["id"]), "nombre": role.capitalize() + " · " + node["id"],
                "tipo": "api" if role in DOMAINS else role, "servicio": role, "nodo": node["id"],
                "dependencias": [role + "_db"] if role in DOMAINS else [],
                "url": f"http://{role}:{internal_port}" if local else base_url(node, port), "origen_configuracion": "inventario_nodos",
                "dominio_fallo_declarado": node["failure_domain"]}
            if role == "compras":
                entry["dependencias_servicio"] = ["eventos"]
            entries.append(entry)
            if role == "gateway":
                for domain in DOMAINS:
                    entries.append({"id": component_id(domain + "_ruta", node["id"]),
                        "nombre": "Ruta " + domain + " · " + node["id"], "tipo": "ruta", "servicio": domain,
                        "nodo": node["id"], "url": f"http://gateway:{document['ports'][domain]}" if local else base_url(node, document["ports"][domain]),
                        "dependencias": [component_id(domain, n["id"]) for n in document["nodes"] if domain in n["roles"]],
                        "politica_dependencias": "al_menos_una_replica_sana_con_sus_dependencias",
                        "origen_configuracion": "inventario_nodos", "dominio_fallo_declarado": node["failure_domain"]})
    return entries


def database_components(document, components, diagnostics, moment):
    result, assignments = [], []
    by_id = {c["id"]: c for c in components}
    for domain, config in document["databases"].items():
        sources = [d for d in diagnostics if by_id.get(d["componente"], {}).get("servicio") == domain]
        available = [d for d in sources if d.get("disponible") is True]
        roles = sorted({d["rol"] for d in available if isinstance(d.get("rol"), str) and d["rol"] in {"primario", "standby"}})
        failed = any(c.get("base_datos") == "no_disponible" for c in components if c.get("servicio") == domain and c["tipo"] == "api")
        identities = {}
        for source in available:
            identity = source.get("identidad") if isinstance(source.get("identidad"), dict) else {}
            system = source.get("sistema") if isinstance(source.get("sistema"), dict) else {}
            checkpoint = source.get("ultimo_checkpoint") if isinstance(source.get("ultimo_checkpoint"), dict) else {}
            name = identity.get("identidad_nodo")
            if source.get("rol") != "primario" or not isinstance(name, str) or not name:
                continue
            matched = next((n["id"] for n in document["nodes"] if "nx_" + component_id(domain, n["id"]) == name), None)
            system_id = system.get("identificador_sistema")
            system_id = system_id if isinstance(system_id, str) and re.fullmatch(r"[0-9]{1,24}", system_id) else None
            identity_key = (name, system_id)
            identities[identity_key] = {"nodo": matched, "identidad_nodo": name,
                "identificador_sistema": system_id, "direccion": identity.get("direccion"),
                "puerto": identity.get("puerto"), "timeline": checkpoint.get("timeline_id"),
                "fuente": source["componente"], "comprobado_en": source.get("comprobado_en"),
                "alcance": "Identidad informada por PostgreSQL; no certifica la máquina física ni fencing."}
        primaries = list(identities.values())
        observed = primaries[0] if len(primaries) == 1 else None
        mismatch = bool(roles and roles != ["primario"]) or len(primaries) > 1 or bool(observed and observed["nodo"] != config["primary"])
        state = "degradado" if failed or mismatch else "saludable" if available and roles == ["primario"] else "desconocido"
        result.append({"id": domain + "_db", "nombre": "PostgreSQL " + domain, "tipo": "postgresql",
            "nodo": config["primary"], "dependencias": [], "estado": state, "comprobado_en": moment,
            "latencia_ms": None, "instancia": None, "base_datos": "disponible" if available else "desconocido",
            "origen_configuracion": "inventario_nodos", "roles_observados": roles,
            "detalle": "Rol e identidad SQL observados mediante las API; nodo configurado e identidad observada se muestran por separado.",
            "discordancia_rol": mismatch})
        latest = max(available, key=lambda source: str(source.get("comprobado_en") or ""), default={})
        # Una sola muestra: no sumar canales capturados en primarios/momentos diferentes.
        rows = latest.get("replicas")
        channels = {row["application_name"]: row for row in rows if isinstance(row, dict)
                    and isinstance(row.get("application_name"), str)} if isinstance(rows, list) and len(primaries) <= 1 else {}
        channel_evidence = (isinstance(rows, list) and latest.get("rol") == "primario" and len(primaries) <= 1
                            and all(isinstance(row, dict) and row.get("application_name") and row.get("state") for row in rows))
        standby_rows = []
        for node_id in config["standbys"]:
            application = "nx_" + component_id(domain, node_id)
            channel = channels.get(application)
            row = {"nodo": node_id, "application_name": application, "canal_observado": bool(channel),
                "estado_canal": channel.get("state") if channel else None,
                "sync_state": channel.get("sync_state") if channel else None,
                "lag_bytes": channel.get("lag_bytes") if channel else None,
                "replay_lsn": channel.get("replay_lsn") if channel else None,
                "replay_lag_seconds": channel.get("replay_lag_seconds") if channel else None,
                "estado_observacion": "activo" if channel and channel.get("state") == "streaming" else "degradado" if channel else "no_disponible" if channel_evidence else "desconocido"}
            standby_rows.append(row)
            result.append({"id": component_id(domain + "_sb", node_id), "nombre": "Standby " + domain + " · " + node_id,
                "tipo": "replica_postgresql", "nodo": node_id, "dependencias": [domain + "_db"],
                "estado": "desconocido", "comprobado_en": moment, "latencia_ms": None, "instancia": application,
                "base_datos": "desconocido", "origen_configuracion": "inventario_nodos",
                "replicacion": row["estado_canal"], "lag_bytes": row["lag_bytes"],
                "detalle": "Canal WAL observado desde la conexión API; salud directa del standby no consultada." if channel else "Standby configurado; sin canal WAL verificable en esta consulta."})
        durability = latest.get("durabilidad") if isinstance(latest.get("durabilidad"), dict) else {}
        sync_value = durability.get("synchronous_standby_names")
        sync_peers = ["nx_" + component_id(domain, node_id) for node_id in config["standbys"]]
        expected_sync = f"ANY {config['sync_quorum']} ({','.join(sync_peers)})" if config["replication"] == "sync" else ""
        policy_matches = isinstance(sync_value, str) and re.sub(r"\s", "", sync_value) == re.sub(r"\s", "", expected_sync)
        acknowledgers = sum(row.get("state") == "streaming" and row.get("sync_state") in ("sync", "quorum")
                           for name, row in channels.items() if name in sync_peers) if channel_evidence else None
        quorum_state = "desconocido" if sync_value is None or not channel_evidence else "discordante" if not policy_matches else "no_requerido" if config["replication"] == "async" else "disponible" if acknowledgers >= config["sync_quorum"] else "insuficiente"
        sync_commit = durability.get("synchronous_commit")
        waits_for_standby = sync_commit in ("on", "remote_write", "remote_apply")
        write_state = "solo_lectura" if durability.get("transaction_read_only") == "on" or roles == ["standby"] else "espera_por_quorum_prevista" if quorum_state == "insuficiente" and waits_for_standby else "no_comprobada"
        primary_component = next(row for row in result if row["id"] == domain + "_db")
        if quorum_state in ("insuficiente", "discordante") or write_state == "solo_lectura":
            primary_component["estado"] = "degradado"
        elif not observed and primary_component["estado"] == "saludable":
            primary_component["estado"] = "desconocido"
        assignments.append({"base": domain, "primario_configurado": config["primary"], "generaciones": config["generations"],
            "replicacion_configurada": config["replication"], "quorum_configurado": config["sync_quorum"],
            "roles_observados": roles, "discordancia_rol": mismatch, "standbys": standby_rows,
            "primario_observado": observed, "primarios_observados": primaries,
            "posible_conflicto_primarios": len(primaries) > 1,
            "quorum_wal": {"configurado": config["sync_quorum"], "observado": acknowledgers,
                "estado": quorum_state, "politica_observada": sync_value,
                "synchronous_commit_observado": sync_commit, "es_consenso": False},
            "escritura": {"estado": write_state,
                "detalle": "Sólo lecturas de configuración y canales WAL; no se ejecuta un commit de prueba. Quórum insuficiente significa falta de confirmadores en la muestra."},
            "promocion_automatica": False, "ubicacion_servidor_conectado_verificada": False,
            "alcance": "Asignación declarada y canales observados; una réplica conectada no acredita recuperación ni fencing."})
    return result, assignments


def _projection(document, removed, components, assignments, kind, target):
    remaining = [n for n in document["nodes"] if n["id"] not in removed]
    replicas = {role: [n["id"] for n in remaining if role in n["roles"]] for role in sorted(ROLES)}
    healthy = {c["id"] for c in components if c.get("estado") == "saludable"}
    databases = []
    for domain, config in document["databases"].items():
        observed = next((row for row in assignments if row["base"] == domain), {})
        copies = [n for n in config["standbys"] if n not in removed]
        if config["primary"] in removed:
            state = "recuperacion_manual_requerida" if copies else "sin_primario_ni_copia_configurada"
        elif config["replication"] == "sync" and len(copies) < config["sync_quorum"]:
            state = "escrituras_bloqueadas_por_quorum"
        else:
            state = "continuidad_condicionada"
        databases.append({"base": domain, "primario_restante": config["primary"] if config["primary"] not in removed else None,
            "standbys_restantes": copies, "estado_escrituras": state, "quorum_configurado": config["sync_quorum"],
            "escritura_observada_actual": observed.get("escritura", {}).get("estado", "no_comprobada"),
            "quorum_wal_actual": observed.get("quorum_wal", {}),
            "recuperacion_manual_proyectada": config["primary"] in removed,
            "quorum_consenso_observado": None,
            "riesgo_datos": "Posible pérdida de WAL no replicado en promoción manual." if config["replication"] == "async" else "Requiere suficientes confirmaciones WAL; un GET saludable no demuestra disponibilidad de escritura."})
    db_states = {db["base"]: db["estado_escrituras"] for db in databases}
    services = []
    for role in sorted(ROLES):
        blockers = []
        if not replicas[role]:
            blockers.append("Sin instancia configurada restante de " + role)
        if role in DOMAINS and db_states[role] != "continuidad_condicionada":
            blockers.append(role + "_db: " + db_states[role])
        if role == "compras":
            if not replicas["eventos"]:
                blockers.append("Sin API Eventos restante")
            if db_states["eventos"] != "continuidad_condicionada":
                blockers.append("eventos_db: " + db_states["eventos"])
        if role == "frontend" and not replicas["gateway"]:
            blockers.append("Sin gateway público restante")
        if role == "gateway" and not replicas["frontend"]:
            blockers.append("Sin frontend restante para el acceso al portal")
        services.append({"servicio": role, "instancias_configuradas_restantes": replicas[role],
            "observadas_saludables_ahora": [n for n in replicas[role] if component_id(role, n) in healthy],
            "estado_proyectado": "dependencias_pendientes" if blockers else "continuidad_condicionada",
            "dependencias_pendientes": blockers})
    return {"tipo": kind, "objetivo": target, "nodos_excluidos": sorted(removed), "servicios": services,
        "bases": databases, "accesos_restantes": [p for p in document["access_points"] if p["node"] not in removed],
        "alcance": "Proyección de configuración, no simulación ejecutada. Depende de red, procesos, almacenamiento y capacidad real de los supervivientes.",
        "capacidad_superviviente_verificada": False}


def infrastructure_view(document, components, assignments):
    nodes = []
    for node in document["nodes"]:
        probes = [c for c in components if c.get("nodo") == node["id"] and c["tipo"] in {"api", "frontend", "gateway", "ruta"}]
        answers = [c for c in probes if c.get("http_status") is not None]
        healthy = [c for c in probes if c["estado"] == "saludable"]
        observation = "respuesta_observada" if probes and len(healthy) == len(probes) else "respuesta_parcial" if answers else "sin_respuesta_verificable" if probes else "sin_sonda_directa"
        nodes.append({**node, "estado_observacion": observation, "estado_fisico": "no_verificado",
            "componentes": [c["id"] for c in probes], "respuestas_http": len(answers), "sondas": len(probes),
            "servicios_saludables": len(healthy), "recursos": "declarados; carga y capacidad disponible no medidas",
            "bases": [{"base": domain, "rol_configurado": "primario" if db["primary"] == node["id"] else "standby"}
                for domain, db in document["databases"].items() if node["id"] == db["primary"] or node["id"] in db["standbys"]]})
    domains = sorted({n["failure_domain"] for n in document["nodes"]})
    dependencies = []
    for role in sorted(ROLES):
        machines = [n for n in document["nodes"] if role in n["roles"]]
        domain_ids = sorted({n["failure_domain"] for n in machines})
        if len(domain_ids) < 2:
            dependencies.append({"componente": role, "motivo": "Sin redundancia en dominios de fallo declarados independientes",
                "nodos": [n["id"] for n in machines], "dominios": domain_ids})
    for domain, db in document["databases"].items():
        dependencies.append({"componente": domain + "_db", "nodos": [db["primary"]],
            "motivo": "Primario de escritura único; si falla, requiere fencing y recuperación manual coordinada." if db["standbys"] else "Primario único sin standby configurado."})
    return {"modo": "inventario_nodos", "despliegue": document["deployment"], "perfil": document["profile"],
        "alta_disponibilidad_verificada": False, "promocion_automatica": False,
        "estado_preparacion": "recuperacion_manual_preparada", "nodos": nodes, "bases": assignments,
        "dominios_fallo_declarados": domains, "dependencias_criticas": dependencies,
        "accesos": document["access_points"],
        "version_declarada": document.get("release"), "inventory_sha256": document.get("inventory_sha256"),
        "cambio_acceso": "Cambio manual a una URL de gateway superviviente. Puede requerir iniciar sesión de nuevo; no se transfieren cookies entre hosts.",
        "proyecciones": [_projection(document, {n["id"]}, components, assignments, "nodo", n["id"]) for n in document["nodes"]]
            + [_projection(document, {n["id"] for n in document["nodes"] if n["failure_domain"] == domain}, components, assignments, "dominio_fallo", domain) for domain in domains],
        "alcance": "El inventario declara máquinas, recursos y dominios de fallo; HTTP no acredita independencia física ni capacidad restante."}


def adapt_scenarios(rows, document):
    """Los escenarios conservan sus IDs, pero se vinculan a las réplicas reales."""
    entries = component_entries(document)
    replacements = {"compras1": "compras", "compras2": "compras", "eventos_a": "eventos", "eventos_b": "eventos", "usuarios_a": "usuarios", "usuarios_b": "usuarios"}
    for row in rows:
        components = []
        for key in row["componentes"]:
            if key in replacements:
                components.extend(e["id"] for e in entries if e["tipo"] == "api" and e["servicio"] == replacements[key])
            elif key in {"frontend", "gateway"}:
                components.extend(e["id"] for e in entries if e["tipo"] == key)
            elif key.endswith("_entrada"):
                components.extend(e["id"] for e in entries if e["tipo"] == "ruta" and e["servicio"] == key.split("_")[0])
            else:
                components.append(key)
        row["componentes"] = list(dict.fromkeys(components))
        if row["id"] in {"B", "C", "D"}:
            domain = {"B": "compras", "C": "eventos", "D": "usuarios"}[row["id"]]
            replicas = [e for e in entries if e["tipo"] == "api" and e["servicio"] == domain]
            row.update({"nombre": "Interrupción de una réplica de " + domain.capitalize(),
                "objetivo": "Observar si otra réplica configurada atiende lecturas a través de un gateway superviviente.",
                "requisitos": f"Inventario con {len(replicas)} réplicas de {domain}; se necesitan al menos dos, dependencias disponibles y capacidad restante.",
                "accion": "El operador elige una réplica real del inventario, realiza la interrupción autorizada fuera del panel y captura evidencia antes/después.",
                "esperado": "La ruta devuelve una instancia superviviente observada; solicitudes en vuelo pueden fallar y necesitar reintento seguro.",
                "limitaciones": ["Sin dos réplicas y dependencias disponibles no hay continuidad. Una lectura HTTP no demuestra continuidad de escrituras."]})
        elif row["id"] == "H":
            row["requisitos"] = "Consultar primarios y standbys configurados y canales WAL observados; procedimiento manual con fencing, una única decisión de promoción y actualización de todas las rutas."
            row["limitaciones"] = ["No hay promoción automática. Si se pierde el primario, hay interrupción hasta recuperación coordinada; replicación asíncrona puede perder WAL no recibido y la síncrona puede bloquear escrituras."]
        elif row["id"] == "I":
            row["componentes"] = [e["id"] for e in entries] + [d + "_db" for d in DOMAINS]
            row["nombre"] = "Interrupción de un nodo o dominio de fallo"
            row["requisitos"] = "Inventario vigente, dominios físicos realmente independientes y un acceso/observador superviviente. Revisar las proyecciones y dependencias del panel."
            row["limitaciones"] = ["La proyección es estática y no ejecuta fallos. Un timeout no acredita caída de máquina; CPU y memoria declaradas no prueban capacidad restante. Gateways alternativos requieren cambio explícito de acceso."]
    return rows

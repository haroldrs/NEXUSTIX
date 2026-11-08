"""Inventario declarativo LAN; leerlo nunca consulta ni modifica una máquina."""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import re
from pathlib import Path

import yaml

DOMAINS = ("usuarios", "eventos", "compras")
ROLES = ("frontend", "gateway", *DOMAINS)
PORTS = {
    "public": 8080, "usuarios": 8001, "eventos": 8002, "compras": 8005,
    "frontend_backend": 8180, "usuarios_backend": 18001,
    "eventos_backend": 18002, "compras_backend": 18003,
    "usuarios_db": 5432, "eventos_db": 5433, "compras_db": 5434,
}
# Límites por contenedor, no una medición de capacidad ni una reserva del host.
SERVICE_RESOURCES = {
    "frontend": {"cpus": 0.25, "memory_mb": 128},
    "gateway": {"cpus": 0.25, "memory_mb": 128},
    "router": {"cpus": 0.25, "memory_mb": 128},
    "usuarios": {"cpus": 0.5, "memory_mb": 384},
    "eventos": {"cpus": 0.5, "memory_mb": 384},
    "compras": {"cpus": 0.5, "memory_mb": 384},
    "postgres": {"cpus": 0.5, "memory_mb": 512},
}
PRIVATE = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
SLUG = re.compile(r"^[a-z][a-z0-9-]*$")
RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class InventoryError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """No acepta claves repetidas ni las fusiones implícitas de YAML."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise InventoryError("Las claves YAML deben ser texto")
        if key in result:
            raise InventoryError(f"Clave YAML repetida: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_inventory(path, *, with_source=False):
    source = Path(path)
    if source.stat().st_size > 256 * 1024:
        raise InventoryError("Inventario demasiado grande; máximo 256 KiB")
    content = source.read_text(encoding="utf-8")
    try:
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(content)):
            raise InventoryError("Use YAML explícito, sin anclas ni alias")
        normalized = validate_inventory(yaml.load(content, Loader=UniqueLoader))
        return (normalized, content) if with_source else normalized
    except yaml.YAMLError as exc:
        raise InventoryError("YAML inválido; no se admiten etiquetas ni fusiones") from exc


def _keys(value, allowed, required, where):
    if not isinstance(value, dict):
        raise InventoryError(f"{where} debe ser un mapa")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown or missing:
        raise InventoryError(f"{where}: claves desconocidas {sorted(unknown)}; faltan {sorted(missing)}")


def _slug(value, where, minimum=1, maximum=20):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or not SLUG.fullmatch(value):
        raise InventoryError(f"{where}: use {minimum}–{maximum} caracteres, letras minúsculas, dígitos y guiones; empiece con letra")
    return value


def _integer(value, where, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InventoryError(f"{where} debe ser entero entre {minimum} y {maximum}")
    return value


def replica_id(domain, node_id):
    return f"nx_{domain}_{node_id.replace('-', '_')}"


def node_resources(node, databases):
    kinds = list(node["roles"])
    if set(DOMAINS).intersection(kinds):
        kinds.append("router")
    kinds.extend("postgres" for db in databases.values() if node["id"] in [db["primary"], *db["standbys"]])
    return {"cpus": round(sum(SERVICE_RESOURCES[k]["cpus"] for k in kinds), 3),
            "memory_mb": sum(SERVICE_RESOURCES[k]["memory_mb"] for k in kinds)}


def _bridge(value, where):
    try:
        network = ipaddress.ip_network(value, strict=True)
    except (ValueError, TypeError) as exc:
        raise InventoryError(f"{where}: se requiere una subred IPv4 privada /24") from exc
    if network.version != 4 or network.prefixlen != 24 or not any(network.subnet_of(part) for part in PRIVATE):
        raise InventoryError(f"{where}: se requiere una subred IPv4 RFC1918 /24")
    return network


def _node_list(value, ids, where, minimum=0):
    if (not isinstance(value, list) or not minimum <= len(value) <= 32
            or any(not isinstance(item, str) or item not in ids for item in value)
            or len(set(value)) != len(value)):
        raise InventoryError(f"{where}: use entre {minimum} y 32 nodos del inventario, sin repetir")
    return value


def _applications(data, ids):
    """Expande sólo aplicaciones; nunca cambia miembros ni roles PostgreSQL.

    Orden deliberado: roles manuales, espejo aditivo y colocación exacta por rol.
    Una réplica es una instancia de un rol en un nodo, no procesos adicionales.
    """
    applications = data.setdefault("applications", {})
    _keys(applications, {"mirror_nodes", "placements", "replicas"}, set(), "applications")
    mirrored = _node_list(applications.setdefault("mirror_nodes", []), ids, "applications.mirror_nodes")
    placements = applications.setdefault("placements", {})
    _keys(placements, ROLES, set(), "applications.placements")
    for role, members in placements.items():
        _node_list(members, ids, f"applications.placements.{role}", minimum=1)
    replicas = applications.setdefault("replicas", {})
    _keys(replicas, ROLES, set(), "applications.replicas")
    for role, count in replicas.items():
        _integer(count, f"applications.replicas.{role}", 1, len(ids))
    for node in data["nodes"]:
        assigned = set(node["roles"])
        if node["id"] in mirrored:
            assigned.update(ROLES)
        for role, members in placements.items():
            if node["id"] in members:
                assigned.add(role)
            else:
                assigned.discard(role)
        node["roles"] = [role for role in ROLES if role in assigned]
    for role, count in replicas.items():
        actual = sum(role in node["roles"] for node in data["nodes"])
        if count != actual:
            raise InventoryError(f"applications.replicas.{role} requiere {count} nodos asignados; hay {actual}. Ajuste roles, mirror_nodes o placements")


def inventory_fingerprint(inventory):
    """Huella de la configuración normalizada; no acredita el código ejecutado."""
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _access(data):
    """Sólo prepara un nombre estable; la infraestructura externa no se crea."""
    access = data.setdefault("access", {"mode": "manual"})
    _keys(access, {"mode", "hostname", "failure_domains"}, {"mode"}, "access")
    if access["mode"] not in {"manual", "external_dns", "external_proxy"}:
        raise InventoryError("access.mode debe ser manual, external_dns o external_proxy")
    if access["mode"] == "manual":
        if set(access) != {"mode"}:
            raise InventoryError("access manual no admite hostname ni failure_domains")
        return
    _keys(access, {"mode", "hostname", "failure_domains"}, {"mode", "hostname", "failure_domains"}, "access")
    hostname = access["hostname"]
    if (not isinstance(hostname, str) or len(hostname) > 253 or "." not in hostname
            or any(not HOST_LABEL.fullmatch(label) for label in hostname.split("."))
            or hostname == "localhost" or hostname.endswith(".localhost")):
        raise InventoryError("access.hostname: nombre DNS minúsculo sin esquema, puerto ni ruta")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise InventoryError("access.hostname debe ser un nombre estable, no la IP de una computadora")
    domains = access["failure_domains"]
    if not isinstance(domains, list) or not 2 <= len(domains) <= 32:
        raise InventoryError("access externo requiere entre 2 y 32 dominios físicos declarados para su infraestructura")
    for domain in domains:
        _slug(domain, "access.failure_domains[]", maximum=40)
    if len(set(domains)) != len(domains):
        raise InventoryError("access.failure_domains no admite duplicados")
    gateway_domains = {node["failure_domain"] for node in data["nodes"] if "gateway" in node["roles"]}
    if len(gateway_domains) < 2:
        raise InventoryError("access externo requiere gateways en al menos dos dominios físicos")


def stable_access(inventory):
    access = inventory["access"]
    result = {**access, "verified": False, "automatic_switch_verified": False,
              "status": "manual" if access["mode"] == "manual" else "external_configuration_required"}
    if access["mode"] != "manual":
        result["url"] = f"http://{access['hostname']}:{inventory['ports']['public']}"
        result["api_urls"] = {domain: f"http://{access['hostname']}:{inventory['ports'][domain]}" for domain in DOMAINS}
    return result


def validate_inventory(data):
    _keys(data, {"version", "deployment", "profile", "gateway", "nodes", "databases", "ports", "applications", "release", "access"},
          {"version", "deployment", "profile", "gateway", "nodes", "databases"}, "inventario")
    data = copy.deepcopy(data)
    _integer(data["version"], "version", 1, 1)
    _slug(data["deployment"], "deployment", 3, 24)
    release = data.setdefault("release", "sin-etiqueta")
    if not isinstance(release, str) or not RELEASE.fullmatch(release):
        raise InventoryError("release: etiqueta declarada de 1–64 caracteres alfanuméricos, punto, guion, + o _")
    if data["profile"] not in {"local", "distribuido", "alta_disponibilidad"}:
        raise InventoryError("profile debe ser local, distribuido o alta_disponibilidad")
    _keys(data["gateway"], {"preferred"}, {"preferred"}, "gateway")
    ports = data.get("ports", {})
    _keys(ports, PORTS, set(), "ports")
    data["ports"] = {**PORTS, **ports}
    for key, port in data["ports"].items():
        _integer(port, f"ports.{key}", 1024, 65535)
    if len(set(data["ports"].values())) != len(data["ports"]):
        raise InventoryError("Los puertos publicados de roles diferentes deben ser distintos")
    nodes = data["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 32:
        raise InventoryError("nodes admite entre 1 y 32 computadoras")
    ids, addresses, fingerprints = set(), set(), set()
    for node in nodes:
        _keys(node, {"id", "address", "platform", "failure_domain", "resources", "roles", "docker_subnet", "postgres_client_cidrs", "identity_fingerprint"},
              {"id", "address", "platform", "failure_domain", "resources", "roles"}, "nodes[]")
        identity = _slug(node["id"], "node.id")
        if "identity_fingerprint" in node and (not isinstance(node["identity_fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", node["identity_fingerprint"])):
            raise InventoryError(f"{identity}.identity_fingerprint requiere SHA-256 hexadecimal de la clave pública Ed25519 verificada por el operador")
        if node.get("identity_fingerprint"):
            if node["identity_fingerprint"] in fingerprints:
                raise InventoryError("Cada nodo debe tener su propia clave de identidad; huella repetida")
            fingerprints.add(node["identity_fingerprint"])
        if identity in ids:
            raise InventoryError(f"Nodo repetido: {identity}")
        ids.add(identity)
        _slug(node["failure_domain"], f"{identity}.failure_domain", maximum=40)
        if node["platform"] not in {"fedora", "wsl2", "linux"}:
            raise InventoryError(f"{identity}.platform debe ser fedora, wsl2 o linux")
        try:
            address = ipaddress.ip_address(node["address"])
        except (ValueError, TypeError) as exc:
            raise InventoryError(f"{identity}.address debe ser IPv4 LAN, sin puerto ni DNS") from exc
        allowed = address.version == 4 and (any(address in part for part in PRIVATE) or
                  data["profile"] == "local" and str(address) == "127.0.0.1")
        if not allowed or str(address) in addresses:
            raise InventoryError(f"{identity}.address debe ser RFC1918 única (local permite 127.0.0.1)")
        addresses.add(str(address))
        node["address"] = str(address)
        # NAT de Docker Desktop/WSL puede cambiar el origen observado por PostgreSQL.
        # Excepciones explícitas por servidor; nunca se abre toda la LAN por defecto.
        client_cidrs = node.setdefault("postgres_client_cidrs", [])
        if not isinstance(client_cidrs, list) or len(client_cidrs) > 64:
            raise InventoryError(f"{identity}.postgres_client_cidrs admite hasta 64 orígenes explícitos")
        for cidr in client_cidrs:
            try:
                network = ipaddress.ip_network(cidr, strict=True)
            except (ValueError, TypeError) as exc:
                raise InventoryError(f"{identity}: CIDR de cliente PostgreSQL inválido") from exc
            if network.version != 4 or network.prefixlen < 24 or not any(network.subnet_of(part) for part in PRIVATE):
                raise InventoryError(f"{identity}: los CIDR extra deben ser privados /24 a /32; prefiera /32")
        resources = node["resources"]
        _keys(resources, {"cpus", "memory_mb"}, {"cpus", "memory_mb"}, f"{identity}.resources")
        cpus = resources["cpus"]
        if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not math.isfinite(cpus) or not 0.25 <= cpus <= 256:
            raise InventoryError(f"{identity}.resources.cpus debe estar entre 0.25 y 256")
        _integer(resources["memory_mb"], f"{identity}.resources.memory_mb", 128, 1048576)
        roles = node["roles"]
        if not isinstance(roles, list) or any(not isinstance(role, str) or role not in ROLES for role in roles) or len(set(roles)) != len(roles):
            raise InventoryError(f"{identity}.roles admite roles únicos: {', '.join(ROLES)}")
    _applications(data, ids)
    gateways = [node["id"] for node in nodes if "gateway" in node["roles"]]
    if data["gateway"]["preferred"] not in gateways:
        raise InventoryError("gateway.preferred debe identificar un nodo con rol gateway")
    for role in ROLES:
        if not any(role in node["roles"] for node in nodes):
            raise InventoryError(f"Falta al menos un nodo con rol {role}")
    _access(data)
    databases = data["databases"]
    _keys(databases, DOMAINS, DOMAINS, "databases")
    for domain, database in databases.items():
        _keys(database, {"primary", "standbys", "replication", "sync_quorum", "generations"},
              {"primary", "standbys", "replication"}, f"databases.{domain}")
        if database["primary"] not in ids:
            raise InventoryError(f"{domain}.primary no es un nodo del inventario")
        standbys = database["standbys"]
        if not isinstance(standbys, list) or any(not isinstance(item, str) or item not in ids for item in standbys):
            raise InventoryError(f"{domain}.standbys debe listar nodos del inventario")
        if len(standbys) != len(set(standbys)) or database["primary"] in standbys:
            raise InventoryError(f"{domain}: cada copia debe ocupar un nodo distinto")
        if database["replication"] not in {"async", "sync"}:
            raise InventoryError(f"{domain}.replication debe ser async o sync")
        members = [database["primary"], *standbys]
        generations = database.setdefault("generations", {})
        _keys(generations, members, set(), f"{domain}.generations")
        for member in members:
            generations.setdefault(member, 1)
            _integer(generations[member], f"{domain}.generations.{member}", 1, 2147483647)
        database.setdefault("sync_quorum", 1 if database["replication"] == "sync" else 0)
        _integer(database["sync_quorum"], f"{domain}.sync_quorum", 0, 31)
        if database["replication"] == "sync" and not 1 <= database["sync_quorum"] <= len(standbys):
            raise InventoryError(f"{domain}: sync requiere suficientes standbys para sync_quorum")
        if database["replication"] == "async" and database["sync_quorum"]:
            raise InventoryError(f"{domain}: async requiere sync_quorum: 0")
    # Cada host usa un puente explícito. Puede personalizarse por VPN/rutas locales.
    used_bridges = []
    for node in nodes:
        if "docker_subnet" not in node:
            fingerprint = hashlib.sha256(f"{data['deployment']}/{node['id']}".encode()).digest()
            node["docker_subnet"] = f"172.{16 + fingerprint[0] % 16}.{1 + fingerprint[1] % 254}.0/24"
        bridge = _bridge(node["docker_subnet"], f"{node['id']}.docker_subnet")
        if any(ipaddress.ip_address(address) in bridge for address in addresses) or any(bridge.overlaps(other) for other in used_bridges):
            raise InventoryError(f"{node['id']}.docker_subnet se solapa con una IP LAN o con otro puente declarado")
        used_bridges.append(bridge)
        allocation = node_resources(node, databases)
        if not allocation["cpus"]:
            raise InventoryError(f"{node['id']} no ejecuta ningún servicio ni base de datos")
        if allocation["cpus"] > node["resources"]["cpus"] or allocation["memory_mb"] > node["resources"]["memory_mb"]:
            raise InventoryError(f"{node['id']}: asignación {allocation} excede resources {node['resources']}; redistribuya roles o aumente presupuesto")
    profile = data["profile"]
    if profile == "local" and len(nodes) != 1:
        raise InventoryError("El perfil local exige exactamente un nodo")
    if profile in {"distribuido", "alta_disponibilidad"} and len(nodes) < 2:
        raise InventoryError("El perfil distribuido exige al menos dos nodos")
    if profile == "distribuido" and not any(sum(role in node["roles"] for node in nodes) >= 2 for role in DOMAINS):
        raise InventoryError("Distribuido requiere al menos un microservicio con dos réplicas; otros roles pueden tener puntos únicos de fallo")
    if profile == "alta_disponibilidad":
        domains = {node["id"]: node["failure_domain"] for node in nodes}
        if len(set(domains.values())) < 3:
            raise InventoryError("Alta disponibilidad preparada requiere tres dominios físicos de fallo independientes")
        for role in ROLES:
            if len({node["failure_domain"] for node in nodes if role in node["roles"]}) < 2:
                raise InventoryError(f"Alta disponibilidad preparada requiere {role} en al menos dos dominios de fallo")
        for name, db in databases.items():
            copies = [db["primary"], *db["standbys"]]
            if len({domains[item] for item in copies}) < 3 or len(db["standbys"]) < 2:
                raise InventoryError(f"{name}: perfil HA requiere primario y dos standbys en tres dominios independientes")
            worst_loss = max(sum(domains[node_id] == failure for node_id in db["standbys"])
                             for failure in set(domains.values()) if failure != domains[db["primary"]])
            if db["replication"] != "sync" or len(db["standbys"]) - worst_loss < db["sync_quorum"]:
                raise InventoryError(f"{name}: perfil HA requiere replicación sync y conservar al menos un standby extra respecto al quórum")
    return data


def topology_document(inventory):
    document = copy.deepcopy(inventory)
    ports = document["ports"]
    document["access_points"] = [
        {"node": node["id"], "url": f"http://{node['address']}:{ports['public']}",
         "preferred": node["id"] == document["gateway"]["preferred"],
         **{f"{domain}_url": f"http://{node['address']}:{ports[domain]}" for domain in DOMAINS}}
        for node in document["nodes"] if "gateway" in node["roles"]
    ]
    for node in document["nodes"]:
        node["reserved_resources"] = node_resources(node, document["databases"])
    document["ha_status"] = "prepared_manual_recovery" if document["profile"] == "alta_disponibilidad" else "not_claimed"
    document["physical_ha_verified"] = False
    document["automatic_failover"] = False
    document["coordination"] = "manual_fencing_required"
    document["inventory_sha256"] = inventory_fingerprint(inventory)
    document["release_is_declarative"] = True
    document["stable_access"] = stable_access(inventory)
    document["application_replicas"] = {
        role: {"configured": sum(role in node["roles"] for node in document["nodes"]),
               "nodes": [node["id"] for node in document["nodes"] if role in node["roles"]],
               "failure_domains": sorted({node["failure_domain"] for node in document["nodes"] if role in node["roles"]})}
        for role in ROLES
    }
    return document

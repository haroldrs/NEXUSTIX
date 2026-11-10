"""Render puro: no ejecuta Docker, SSH, sondeos, migraciones ni failover."""
from __future__ import annotations

import json

from inventory import DOMAINS, PORTS, SERVICE_RESOURCES, inventory_fingerprint, replica_id, stable_access, topology_document

INTERNAL_PORTS = {"frontend": 80, "usuarios": 8001, "eventos": 8002, "compras": 8003}
SECRET_FILES = {
    "jwt_public": "jwt-public.pem", "jwt_private": "jwt-private.pem",
    "admin_password": "admin-password", "eventos_service_token": "eventos-service-token",
    **{f"{domain}_database_url": f"{domain}-database-url" for domain in DOMAINS},
    **{f"{domain}_{kind}_password": f"{domain}-{kind.replace('_', '-')}-password"
       for domain in DOMAINS for kind in ("db", "admin", "replication")},
}


def _server_address(inventory, current, backend, service):
    if current["id"] == backend["id"]:
        return f"{service}:{INTERNAL_PORTS[service]}"
    return f"{backend['address']}:{inventory['ports'][f'{service}_backend']}"


def haproxy_config(inventory, node, public):
    """Una vista explícita del inventario; los healthchecks deciden disponibilidad."""
    rows = [
        "global", "    log stdout format raw local0", "    maxconn 1024",
        "defaults", "    log global", "    mode http", "    option httplog",
        "    timeout connect 3s", "    timeout client 60s", "    timeout server 60s",
        "    retries 0", "",
        "resolvers docker", "    nameserver embedded 127.0.0.11:53", "    resolve_retries 3",
        "    timeout resolve 1s", "    timeout retry 1s", "    hold valid 10s", "",
        "# No redispatch ni reintento de escrituras: el cliente conserva su clave de idempotencia.",
    ]
    gateway_health = json.dumps({"status": "ok", "servicio": "gateway", "node_id": node["id"],
                                 "deployment_id": inventory["deployment"], "release": inventory["release"],
                                 "inventory_sha256": inventory_fingerprint(inventory)}, separators=(",", ":"))
    roles = ("frontend", *DOMAINS) if public else DOMAINS
    for role in roles:
        port = inventory["ports"]["public" if role == "frontend" else role] if public else PORTS[role]
        rows.extend([
            "", f"frontend entrada_{role}", f"    bind :{port}",
            f"    http-request return status 200 content-type application/json string '{gateway_health}' if {{ path /lb-health }}",
        ])
        if public and role in DOMAINS:
            rows.extend([
                "    acl consulta_cluster path /admin/operaciones/cluster /diagnostico/instancia",
                "    acl consulta_cluster path_beg /admin/operaciones/cluster/",
                f"    use_backend observadores_{role} if consulta_cluster",
            ])
        rows.extend([
            f"    default_backend pool_{role}", "", f"backend pool_{role}",
            "    balance roundrobin", "    option httpchk",
            "    http-check send meth GET uri /health ver HTTP/1.1 hdr Host nexustix.internal",
            "    http-check expect status 200", "    http-response set-header X-Nexustix-Backend %[srv_name]",
        ])
        for backend in inventory["nodes"]:
            if role in backend["roles"]:
                address = _server_address(inventory, node, backend, role)
                resolution = " resolvers docker resolve-prefer ipv4 init-addr last,libc,none" if node["id"] == backend["id"] else ""
                rows.append(f"    server {role}_{backend['id']} {address} check inter 3s fall 2 rise 2{resolution}")
        if public and role in DOMAINS:
            rows.extend([
                "", f"backend observadores_{role}",
                "    # Afinidad por origen sólo para caché/historial; no autentica al cliente.",
                "    balance source", "    option httpchk",
                "    http-check send meth GET uri /health/observador ver HTTP/1.1 hdr Host nexustix.internal",
                "    http-check expect status 200", "    http-response set-header X-Nexustix-Backend %[srv_name]",
            ])
            for backend in inventory["nodes"]:
                if role in backend["roles"]:
                    address = _server_address(inventory, node, backend, role)
                    resolution = " resolvers docker resolve-prefer ipv4 init-addr last,libc,none" if node["id"] == backend["id"] else ""
                    rows.append(f"    server observador_{role}_{backend['id']} {address} check inter 3s fall 2 rise 2{resolution}")
    return "\n".join(rows) + "\n"


def frontend_config(inventory, installation_id=None):
    ports = inventory["ports"]
    return (
        "// Configuración pública. El navegador utiliza el gateway por el que ingresó.\n"
        "const CONFIG = Object.freeze({\n"
        f"  USUARIOS_URL: `${{location.protocol}}//${{location.hostname}}:{ports['usuarios']}`,\n"
        f"  EVENTOS_URL: `${{location.protocol}}//${{location.hostname}}:{ports['eventos']}`,\n"
        f"  COMPRAS_LB_URL: `${{location.protocol}}//${{location.hostname}}:{ports['compras']}`,\n"
        f"  COMPRAS_R1_URL: `${{location.protocol}}//${{location.hostname}}:{ports['compras']}`,\n"
        f"  COMPRAS_R2_URL: `${{location.protocol}}//${{location.hostname}}:{ports['compras']}`,\n"
        f"  PUBLIC_PORTAL_URL: `${{location.protocol}}//${{location.hostname}}:{ports['public']}`,\n"
        f"  DEPLOYMENT_PROFILE: {json.dumps(inventory['profile'])},\n"
        f"  DEPLOYMENT_ID: {json.dumps(inventory['deployment'])},\n"
        f"  INSTALLATION_ID: {json.dumps(installation_id)},\n"
        f"  RELEASE: {json.dumps(inventory['release'])},\n"
        f"  NODE_COUNT: {len(inventory['nodes'])},\n"
        "  CHECKOUT_ORDERS: true,\n"
        "  ...(window.NEXUSTIX_CONFIG || {}),\n"
        "});\n"
        "window.NEXUSTIX_CONFIG = CONFIG;\n"
    )


def render_node(inventory, node, source_root, installation_id=None):
    """Devuelve Compose y archivos públicos relativos al directorio de un nodo."""
    project = f"nexustix-nodes-{inventory['deployment']}-{node['id']}"
    # La misma carpeta generada puede viajar a Fedora o WSL con otra ruta de código.
    source_context = "${NEXUSTIX_SOURCE_ROOT:?Defina la ruta absoluta del repositorio en esta PC}"
    bind_ip = "0.0.0.0" if node["platform"] == "wsl2" else node["address"]
    ports = inventory["ports"]
    nodes = {entry["id"]: entry for entry in inventory["nodes"]}
    labels = {"org.nexustix.deployment": inventory["deployment"], "org.nexustix.node": node["id"],
              "org.nexustix.profile": inventory["profile"], "org.nexustix.managed-by": "despliegue-nodos",
              "org.nexustix.inventory-sha256": inventory_fingerprint(inventory), "org.nexustix.release": inventory["release"]}
    compose = {
        "name": project, "services": {},
        "networks": {"node": {"name": f"{project}-network", "labels": labels,
                                  "ipam": {"config": [{"subnet": node["docker_subnet"]}]}}},
        "volumes": {}, "secrets": {},
    }
    files = {"config/operations-inventory.json": json.dumps(topology_document(inventory), ensure_ascii=False, indent=2) + "\n"}

    def service(kind):
        limits = SERVICE_RESOURCES[kind]
        return {"restart": "unless-stopped", "networks": ["node"], "labels": dict(labels),
                "cpus": limits["cpus"], "mem_limit": f"{limits['memory_mb']}m", "pids_limit": 256,
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}

    def attach_secrets(config, names):
        config["secrets"] = names
        for name in names:
            compose["secrets"][name] = {"file": f"./secrets/{SECRET_FILES[name]}"}

    def build(config, name, dockerfile):
        config["image"] = f"{project}/{name}:local"
        config["build"] = {"context": source_context, "dockerfile": dockerfile}

    def api_health(port):
        return {"test": ["CMD", "python", "-m", "shared.healthcheck", f"http://127.0.0.1:{port}/health"],
                "interval": "5s", "timeout": "4s", "retries": 12, "start_period": "60s"}

    def haproxy(kind, filename, public):
        config = service(kind)
        config["image"] = "haproxy:3.0-alpine"
        config["volumes"] = [f"./config/{filename}:/usr/local/etc/haproxy/haproxy.cfg:ro,z"]
        # Valida que el proceso admite su configuración; no declara sanos sus backends.
        config["healthcheck"] = {"test": ["CMD", "haproxy", "-c", "-f", "/usr/local/etc/haproxy/haproxy.cfg"],
                                 "interval": "10s", "timeout": "4s", "retries": 3}
        if public:
            config["ports"] = [f"{bind_ip}:{ports[name]}:{ports[name]}" for name in ("public", *DOMAINS)]
        files[f"config/{filename}"] = haproxy_config(inventory, node, public)
        compose["services"][kind] = config

    if "gateway" in node["roles"]:
        haproxy("gateway", "haproxy.cfg", public=True)
    if set(DOMAINS).intersection(node["roles"]):
        haproxy("router", "router.cfg", public=False)
    if "frontend" in node["roles"]:
        frontend = service("frontend")
        build(frontend, "frontend", "frontend/Dockerfile")
        frontend["ports"] = [f"{bind_ip}:{ports['frontend_backend']}:80"]
        frontend["volumes"] = ["./config/frontend-config.js:/run/secrets/frontend_config:ro,z"]
        frontend["healthcheck"] = {"test": ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1/health || exit 1"],
                                   "interval": "5s", "timeout": "4s", "retries": 5}
        files["config/frontend-config.js"] = frontend_config(inventory, installation_id=installation_id)
        compose["services"]["frontend"] = frontend

    origins = [f"http://{item['address']}:{ports['public']}" for item in inventory["nodes"] if "gateway" in item["roles"]]
    stable = stable_access(inventory)
    if stable.get("url"):
        origins.append(stable["url"])
    origins = ",".join(origins)
    for domain in DOMAINS:
        if domain not in node["roles"]:
            continue
        config = service(domain)
        build(config, domain, f"{domain}/Dockerfile")
        env = {
            "DATABASE_URL_FILE": f"/run/secrets/{domain}_database_url",
            "JWT_PUBLIC_KEY_FILE": "/run/secrets/jwt_public", "FRONTEND_ORIGINS": origins,
            "DB_APPLICATION_NAME": f"{domain}_{node['id']}", "INSTANCE_ID": f"{domain}_{node['id']}",
            "NEXUSTIX_DEPLOYMENT_ENV": "nodes", "NEXUSTIX_TEST_CRASH_HOOKS": "0",
            "NEXUSTIX_NODE_ID": node["id"], "DB_CONNECT_TIMEOUT_SECONDS": "3",
            "NEXUSTIX_SERVICE": domain,
            "NEXUSTIX_DEPLOYMENT_ID": inventory["deployment"], "NEXUSTIX_RELEASE": inventory["release"],
            "NEXUSTIX_DEPLOYMENT_PROFILE": inventory["profile"],
            "NEXUSTIX_INSTALLATION_ID": installation_id or "",
            "NEXUSTIX_NODE_COUNT": str(len(inventory["nodes"])),
            "NEXUSTIX_INVENTORY_SHA256": inventory_fingerprint(inventory),
            "NEXUSTIX_OPERATIONS_INVENTORY_FILE": "/run/config/operations-inventory.json",
            "USUARIOS_URL": "http://router:8001", "EVENTOS_URL": "http://router:8002",
        }
        credentials = [f"{domain}_database_url", "jwt_public"]
        if domain == "usuarios":
            env.update({"JWT_PRIVATE_KEY_FILE": "/run/secrets/jwt_private", "AUTH_COOKIE_SECURE": "true" if stable.get("url", "").startswith("https://") else "false",
                        "ACCESS_TOKEN_TTL_SECONDS": "600", "REFRESH_TOKEN_TTL_DAYS": "1"})
            credentials += ["jwt_private", "admin_password"]
        else:
            env["EVENTOS_SERVICE_TOKEN_FILE"] = "/run/secrets/eventos_service_token"
            credentials.append("eventos_service_token")
        if domain == "eventos":
            env["NEXUSTIX_SEED_EVENTS"] = "0"
        if domain == "compras":
            env.update({"PURCHASE_RECOVERY_INTERVAL_SECONDS": "10", "PURCHASE_RECOVERY_AUTO_RELEASE": "0",
                        "PUBLIC_PORTAL_URL": stable.get("url") or (origins[0] if origins else "")})
        config["environment"] = env
        attach_secrets(config, credentials)
        config["ports"] = [f"{bind_ip}:{ports[f'{domain}_backend']}:{INTERNAL_PORTS[domain]}"]
        config["volumes"] = ["./config/operations-inventory.json:/run/config/operations-inventory.json:ro,z"]
        config["healthcheck"] = api_health(INTERNAL_PORTS[domain])
        config["depends_on"] = {"router": {"condition": "service_started"}}
        compose["services"][domain] = config

    for domain, database in inventory["databases"].items():
        if node["id"] not in [database["primary"], *database["standbys"]]:
            continue
        primary = nodes[database["primary"]]
        is_primary = node["id"] == database["primary"]
        generation = database["generations"][node["id"]]
        config = service("postgres")
        build(config, "postgres", "despliegue-nodos/postgres/Dockerfile")
        replica_names = [replica_id(domain, item) for item in database["standbys"]]
        config["environment"] = {
            "POSTGRES_DB": f"{domain}_db", "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD_FILE": f"/run/secrets/{domain}_admin_password",
            "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256 --data-checksums",
            "PGDATA": "/var/lib/postgresql/data/pgdata",
            "NEXUSTIX_PG_ROLE": "primary" if is_primary else "standby",
            "NEXUSTIX_PG_DOMAIN": domain, "NEXUSTIX_PG_NODE_ID": replica_id(domain, node["id"]),
            "NEXUSTIX_PG_PRIMARY_ID": replica_id(domain, database["primary"]),
            "NEXUSTIX_PG_PRIMARY_HOST": f"{domain}_db" if is_primary else primary["address"],
            "NEXUSTIX_PG_PRIMARY_PORT": "5432" if is_primary else str(ports[f"{domain}_db"]),
            "NEXUSTIX_PG_ALLOWED_CIDRS": ",".join(dict.fromkeys([*(f"{item['address']}/32" for item in inventory["nodes"]), node["docker_subnet"], *node.get("postgres_client_cidrs", [])])),
            "NEXUSTIX_PG_REPLICATION_SLOTS": ",".join(replica_names),
            "NEXUSTIX_PG_SYNCHRONOUS_STANDBYS": ",".join(replica_names) if database["replication"] == "sync" else "",
            "NEXUSTIX_PG_SYNCHRONOUS_QUORUM": str(database["sync_quorum"]),
            "NEXUSTIX_PG_APP_PASSWORD_FILE": f"/run/secrets/{domain}_db_password",
            "NEXUSTIX_PG_REPLICATION_PASSWORD_FILE": f"/run/secrets/{domain}_replication_password",
            "NEXUSTIX_WAL_ARCHIVE": "/var/lib/postgresql/wal_archive",
        }
        attach_secrets(config, [f"{domain}_admin_password", f"{domain}_db_password", f"{domain}_replication_password"])
        config["ports"] = [f"{bind_ip}:{ports[f'{domain}_db']}:5432"]
        config["volumes"] = []
        for suffix, target in (("data", "/var/lib/postgresql/data"), ("state", "/var/lib/nexustix-pg"), ("wal", "/var/lib/postgresql/wal_archive")):
            volume = f"{domain}_{suffix}_g{generation}"
            compose["volumes"][volume] = {"name": f"{project}-{volume}", "labels": dict(labels)}
            config["volumes"].append(f"{volume}:{target}")
        config["healthcheck"] = {"test": ["CMD", "nexustix-healthcheck.sh"], "interval": "5s",
                                 "timeout": "5s", "retries": 12, "start_period": "30s"}
        compose["services"][f"{domain}_db"] = config
    if not compose["volumes"]:
        del compose["volumes"]
    if not compose["secrets"]:
        del compose["secrets"]
    return {"compose": compose, "files": files}

"""Render puro: no ejecuta Docker, SSH, sondeos, migraciones ni failover."""
from __future__ import annotations

import json

import yaml

from inventory import (DOMAINS, PORTS, SERVICE_RESOURCES, coordination_voters, inventory_fingerprint, patroni_enabled,
                       replica_id, stable_access, topology_document)

ETCD_IMAGE = "gcr.io/etcd-development/etcd:v3.5.17"

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


def etcd_cluster(inventory):
    """Lista "nombre=url" de los votantes; igual en todos los nodos."""
    nodes = {entry["id"]: entry for entry in inventory["nodes"]}
    peer = inventory["ports"]["etcd_peer"]
    return ",".join(f"{voter}=http://{nodes[voter]['address']}:{peer}" for voter in coordination_voters(inventory))


def etcd_hosts(inventory, node):
    """El votante local se alcanza por DNS Docker (evita el hairpin de Windows)."""
    nodes = {entry["id"]: entry for entry in inventory["nodes"]}
    client = inventory["ports"]["etcd_client"]
    return [f"etcd:{client}" if voter == node["id"] else f"{nodes[voter]['address']}:{client}"
            for voter in coordination_voters(inventory)]


def patroni_scope(inventory, domain):
    return f"nexustix-{inventory['deployment']}-{domain}"


def pg_allowed_cidrs(inventory, node):
    return list(dict.fromkeys([*(f"{item['address']}/32" for item in inventory["nodes"]), node["docker_subnet"],
                               *node.get("postgres_client_cidrs", [])]))


def patroni_config(inventory, node, domain):
    """Configuración Patroni de una base en un nodo.

    PostgreSQL y la API REST escuchan dentro del contenedor en el mismo puerto
    que publican, así la dirección anunciada sirve igual desde otros nodos.
    """
    ports = inventory["ports"]
    database = inventory["databases"][domain]
    pg_port, rest_port = ports[f"{domain}_db"], ports[f"{domain}_patroni"]
    sync = database["replication"] == "sync"
    hba = ["local all all scram-sha-256", "host all all 127.0.0.1/32 scram-sha-256",
           "host replication replicator 127.0.0.1/32 scram-sha-256"]
    for cidr in pg_allowed_cidrs(inventory, node):
        hba += [f"host replication replicator {cidr} scram-sha-256", f"host all all {cidr} scram-sha-256"]
    return {
        "scope": patroni_scope(inventory, domain),
        "name": replica_id(domain, node["id"]),
        "restapi": {"listen": f"0.0.0.0:{rest_port}", "connect_address": f"{node['address']}:{rest_port}"},
        "etcd3": {"hosts": etcd_hosts(inventory, node), "protocol": "http"},
        "bootstrap": {
            "dcs": {
                # La principal renueva su "llave" cada loop_wait; si deja de hacerlo
                # durante ttl segundos, la mayoría de votantes permite promover otra.
                "ttl": 30, "loop_wait": 10, "retry_timeout": 10,
                "maximum_lag_on_failover": 16 * 1024 * 1024,
                "synchronous_mode": sync,
                # No estricto: si no queda réplica, la principal sigue vendiendo en
                # modo asíncrono en vez de bloquear cada compra.
                "synchronous_mode_strict": False,
                "synchronous_node_count": max(1, database["sync_quorum"]),
                "postgresql": {
                    "use_pg_rewind": True, "use_slots": True,
                    "parameters": {"wal_level": "replica", "hot_standby": "on", "wal_log_hints": "on",
                                   "max_wal_senders": 10, "max_replication_slots": 10, "wal_keep_size": "512MB",
                                   "max_connections": 100, "password_encryption": "scram-sha-256"},
                },
            },
            "initdb": [{"encoding": "UTF8"}, "data-checksums", {"auth-host": "scram-sha-256"}, {"auth-local": "scram-sha-256"}],
            "post_bootstrap": "/usr/local/bin/nexustix-post-bootstrap",
        },
        "postgresql": {
            "listen": f"0.0.0.0:{pg_port}", "connect_address": f"{node['address']}:{pg_port}",
            "data_dir": "/var/lib/postgresql/data/pgdata", "bin_dir": "/usr/lib/postgresql/15/bin",
            "pgpass": "/tmp/pgpass", "pg_hba": hba,
            # Checkpoint inmediato: una réplica nueva se clona en segundos, no en minutos.
            "basebackup": [{"checkpoint": "fast"}],
            "authentication": {"superuser": {"username": "postgres"}, "replication": {"username": "replicator"},
                               "rewind": {"username": "postgres"}},
            "parameters": {"unix_socket_directories": "/run/postgresql"},
        },
        "tags": {"nofailover": False, "noloadbalance": False, "clonefrom": False, "nosync": False},
    }


def coordination_peers(inventory, current):
    """Miembros del directorio y de la elección Bully: todas las APIs del clúster.

    La prioridad sigue el orden del inventario (el primer nodo es el preferido
    como coordinador), así se ve el relevo cuando cae la PC principal.
    """
    peers, total = [], len(inventory["nodes"])
    for index, node in enumerate(inventory["nodes"]):
        for rank, domain in enumerate(DOMAINS):
            if domain not in node["roles"]:
                continue
            url = (f"http://{domain}:{INTERNAL_PORTS[domain]}" if node["id"] == current["id"]
                   else f"http://{node['address']}:{inventory['ports'][f'{domain}_backend']}")
            peers.append({"id": f"{domain}_{node['id']}", "servicio": domain, "nodo": node["id"], "url": url,
                          "prioridad": (total - index) * 10 + rank})
    return peers


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
        f"  GATEWAYS: {json.dumps([item['address'] for item in inventory['nodes'] if 'gateway' in item['roles']])},\n"
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
        env["NEXUSTIX_PEERS"] = json.dumps(coordination_peers(inventory, node), separators=(",", ":"))
        if patroni_enabled(inventory):
            endpoints = {}
            for base, database in inventory["databases"].items():
                members = [database["primary"], *database["standbys"]]
                rest = ports[f"{base}_patroni"]
                endpoints[base] = [f"http://{base}_db:{rest}" if member == node["id"] else f"http://{nodes[member]['address']}:{rest}"
                                   for member in sorted(members, key=lambda member: member != node["id"])]
            env["NEXUSTIX_COORDINATION_ENDPOINTS"] = json.dumps(endpoints)
        config["environment"] = env
        attach_secrets(config, credentials)
        config["ports"] = [f"{bind_ip}:{ports[f'{domain}_backend']}:{INTERNAL_PORTS[domain]}"]
        config["volumes"] = ["./config/operations-inventory.json:/run/config/operations-inventory.json:ro,z"]
        config["healthcheck"] = api_health(INTERNAL_PORTS[domain])
        config["depends_on"] = {"router": {"condition": "service_started"}}
        compose["services"][domain] = config

    if node["id"] in coordination_voters(inventory):
        etcd = service("etcd")
        client, peer = ports["etcd_client"], ports["etcd_peer"]
        etcd["image"] = ETCD_IMAGE
        etcd["command"] = [
            "etcd", f"--name={node['id']}", "--data-dir=/etcd-data",
            f"--listen-client-urls=http://0.0.0.0:{client}", f"--advertise-client-urls=http://{node['address']}:{client}",
            f"--listen-peer-urls=http://0.0.0.0:{peer}", f"--initial-advertise-peer-urls=http://{node['address']}:{peer}",
            f"--initial-cluster={etcd_cluster(inventory)}", f"--initial-cluster-token={patroni_scope(inventory, 'etcd')}",
            "--initial-cluster-state=new", "--heartbeat-interval=250", "--election-timeout=2500",
        ]
        etcd["ports"] = [f"{bind_ip}:{client}:{client}", f"{bind_ip}:{peer}:{peer}"]
        compose["volumes"]["etcd_data"] = {"name": f"{project}-etcd-data", "labels": dict(labels)}
        etcd["volumes"] = ["etcd_data:/etcd-data"]
        etcd["healthcheck"] = {"test": ["CMD", "etcdctl", f"--endpoints=http://127.0.0.1:{client}", "endpoint", "health"],
                               "interval": "5s", "timeout": "4s", "retries": 12}
        compose["services"]["etcd"] = etcd

    for domain, database in inventory["databases"].items():
        if node["id"] not in [database["primary"], *database["standbys"]]:
            continue
        if patroni_enabled(inventory):
            is_primary = node["id"] == database["primary"]
            pg_port, rest_port = ports[f"{domain}_db"], ports[f"{domain}_patroni"]
            config = service("postgres")
            build(config, "patroni", "despliegue-nodos/patroni/Dockerfile")
            config["environment"] = {
                "NEXUSTIX_PG_ROLE": "primary" if is_primary else "standby",
                "NEXUSTIX_PG_DATABASE": f"{domain}_db", "NEXUSTIX_PATRONI_PORT": str(rest_port),
                "NEXUSTIX_PATRONI_PREFERRED": "1" if is_primary else "0",
                "NEXUSTIX_PATRONI_PREFERRED_URL": f"http://{nodes[database['primary']]['address']}:{rest_port}",
                "NEXUSTIX_PG_ADMIN_PASSWORD_FILE": f"/run/secrets/{domain}_admin_password",
                "NEXUSTIX_PG_APP_PASSWORD_FILE": f"/run/secrets/{domain}_db_password",
                "NEXUSTIX_PG_REPLICATION_PASSWORD_FILE": f"/run/secrets/{domain}_replication_password",
            }
            attach_secrets(config, [f"{domain}_admin_password", f"{domain}_db_password", f"{domain}_replication_password"])
            files[f"config/patroni-{domain}.yml"] = yaml.safe_dump(patroni_config(inventory, node, domain), sort_keys=False)
            generation = database["generations"][node["id"]]
            volume = f"{domain}_patroni_g{generation}"
            compose["volumes"][volume] = {"name": f"{project}-{volume}", "labels": dict(labels)}
            config["volumes"] = [f"{volume}:/var/lib/postgresql/data",
                                 f"./config/patroni-{domain}.yml:/etc/nexustix/patroni.yml:ro,z"]
            config["ports"] = [f"{bind_ip}:{pg_port}:{pg_port}", f"{bind_ip}:{rest_port}:{rest_port}"]
            config["healthcheck"] = {"test": ["CMD", "nexustix-patroni-health"], "interval": "5s",
                                     "timeout": "5s", "retries": 24, "start_period": "60s"}
            if "etcd" in compose["services"]:
                config["depends_on"] = {"etcd": {"condition": "service_started"}}
            compose["services"][f"{domain}_db"] = config
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

#!/usr/bin/env python3
"""Genera manifiestos reproducibles reutilizando servicios de staging; solo archivos."""
from copy import deepcopy
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
SECRET_ENV = "${NEXUSTIX_DISTRIBUTED_SECRETS_DIR:?Defina una carpeta NUEVA de secretos}"
BASES = ("usuarios", "eventos", "compras")


def document(services, name, volumes, vm=False):
    required = {s for service in services.values() for s in service.get("secrets", [])}
    source = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())
    secret_files = {key: value["file"].rsplit("/", 1)[1] for key, value in source["secrets"].items()}
    secret_files["replication_password"] = "replication-password"
    for service in services.values():
        service["networks"] = ["distributed"]
        if "build" in service:
            service["build"]["context"] = ".." if vm else "."
        if "ports" in service and vm:
            service["ports"] = [p.replace("127.0.0.1", "${VM_BIND_IP:?Defina la IP privada de esta VM}") for p in service["ports"]]
    return {"name": name, "services": services, "volumes": {v: {"name": name + "_" + v} for v in volumes},
            "networks": {"distributed": {"name": name + "-network"}},
            "secrets": {s: {"file": SECRET_ENV + "/" + secret_files[s]} for s in sorted(required)}}


def generate(only_vm=False):
    source = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())
    services = deepcopy(source["services"])
    volumes = set()
    for name, svc in services.items():
        if "command" in svc:
            svc["command"] = list(svc["command"])
        svc["restart"] = "unless-stopped"
        svc["image"] = svc["image"].replace("nexustix-staging/", "nexustix-distributed/")
        svc["ports"] = [p.replace(":1808", ":2808") for p in svc.get("ports", [])]
        env = svc.setdefault("environment", {})
        if "FRONTEND_ORIGINS" in env:
            env["FRONTEND_ORIGINS"] = "http://localhost:28080,http://127.0.0.1:28080"
        for key in list(env):
            if "TEST" in key or "STAGING" in str(env[key]):
                if key.startswith("NEXUSTIX_TEST"):
                    env[key] = "0"
                elif key == "ACCESS_TOKEN_TTL_SECONDS":
                    env[key] = "600"
                elif key == "PURCHASE_RECOVERY_INTERVAL_SECONDS":
                    env[key] = "10"
        env["NEXUSTIX_DEPLOYMENT_ENV"] = "distributed"
        for mount in svc.get("volumes", []):
            volumes.add(mount.split(":")[0])
    for base in BASES:
        name = base + "_db"
        primary = services[name]
        env = primary["environment"]
        env.update(NEXUSTIX_INIT_REPLICATION="1", NEXUSTIX_REPLICA_SLOT=name + "_replica",
                   NEXUSTIX_REPLICATION_PASSWORD_FILE="/run/secrets/replication_password")
        primary["secrets"].append("replication_password")
        primary["command"] += ["-c", "wal_level=replica", "-c", "max_wal_senders=10", "-c", "max_replication_slots=10",
                               "-c", "max_slot_wal_keep_size=1024MB", "-c", "wal_log_hints=on",
                               "-c", "checkpoint_timeout=5min", "-c", "archive_mode=on", "-c", "archive_timeout=60s",
                               "-c", "archive_command=archive-wal.sh %p %f"]
        primary["volumes"].append(f"{base}_wal:/var/lib/postgresql/archive")
        volumes.add(base + "_wal")
        replica = deepcopy(primary)
        replica["environment"].pop("NEXUSTIX_INIT_REPLICATION")
        replica["environment"].update(NEXUSTIX_PG_UPSTREAM_HOST=name, NEXUSTIX_PG_SLOT=name + "_replica")
        replica["command"] = ["postgres", "-c", "wal_level=replica", "-c", "max_replication_slots=10",
                              "-c", "max_wal_senders=10", "-c", "hot_standby=on"]
        replica["volumes"] = [f"{base}_replica_data:/var/lib/postgresql/data"]
        replica["depends_on"] = {name: {"condition": "service_healthy"}}
        replica["healthcheck"]["start_period"] = "120s"
        services[name + "_replica"] = replica
        volumes.add(base + "_replica_data")
    lab = document(deepcopy(services), "nexustix-distributed-lab", volumes)
    if not only_vm:
        (ROOT / "docker-compose.distribuido.yml").write_text("# Generado por scripts/generar_topologia.py; entorno NUEVO.\n" + yaml.safe_dump(lab, sort_keys=False))
    out = ROOT / "despliegue-vm"; out.mkdir(exist_ok=True)
    apps = {k: deepcopy(v) for k, v in services.items() if "_db" not in k}
    for name, svc in apps.items():
        svc["ports"] = [p.replace(":28080:", ":8080:").replace(":28081:", ":8001:").replace(":28082:", ":8002:")
                         .replace(":28083:", ":8003:").replace(":28084:", ":8004:").replace(":28085:", ":8005:") for p in svc["ports"]]
        svc["depends_on"] = {k: v for k, v in svc.get("depends_on", {}).items() if "_db" not in k}
        if "FRONTEND_ORIGINS" in svc["environment"]:
            svc["environment"]["FRONTEND_ORIGINS"] = "http://${VM_APP_HOST:?Defina host de aplicación}:8080"
    docs = {"compose.app.yml": document(apps, "nexustix-vm-app", set(), True)}
    for kind in ("primary", "standby"):
        nodes, mounts = {}, set()
        for index, base in enumerate(BASES):
            name = base + "_db" + ("_replica" if kind == "standby" else "")
            svc = deepcopy(services[name]); svc.pop("depends_on", None)
            svc["ports"] = [f"127.0.0.1:{5432+index}:5432"]
            if kind == "standby":
                svc["environment"].update(NEXUSTIX_PG_UPSTREAM_HOST="${VM_PRIMARY_HOST:?Defina primario}", NEXUSTIX_PG_UPSTREAM_PORT=str(5432+index))
            nodes[name] = svc
            mounts.update(m.split(":")[0] for m in svc["volumes"])
        docs[f"compose.{kind}.yml"] = document(nodes, "nexustix-vm-" + kind, mounts, True)
    for name, doc in docs.items():
        for svc in doc["services"].values():
            svc["image"] = svc["image"].replace("nexustix-distributed/", "nexustix-vm/")
            svc["environment"]["NEXUSTIX_DEPLOYMENT_ENV"] = "vm"
            if "POSTGRES_DB" in svc["environment"]:
                svc["build"]["dockerfile"] = "despliegue-vm/postgres/Dockerfile"
                svc["environment"]["PGDATA"] = "/var/lib/postgresql/data/cluster"
                for host in ("VM_APP_HOST", "VM_PRIMARY_HOST", "VM_STANDBY_HOST"):
                    svc["environment"][host] = "${" + host + ":?Defina IP privada}"
                svc["command"] += ["-c", "wal_sender_timeout=15s", "-c", "wal_receiver_timeout=15s"]
                if name == "compose.standby.yml":
                    base = svc["environment"]["POSTGRES_DB"].removesuffix("_db")
                    wal = base + "_wal"
                    svc["volumes"].append(f"{wal}:/var/lib/postgresql/archive")
                    doc["volumes"][wal] = {"name": "nexustix-vm-standby_" + wal}
                    svc["command"] += ["-c", "archive_mode=on", "-c", "archive_timeout=60s",
                        "-c", "archive_command=archive-wal.sh %p %f", "-c", "max_slot_wal_keep_size=1024MB",
                        "-c", "wal_log_hints=on", "-c", "checkpoint_timeout=5min"]
        (out / name).write_text("# Preparación: no desplegado en VMs.\n" + yaml.safe_dump(doc, sort_keys=False))
    # Se ejecuta en la antigua VM primary, después de aislar sus contenedores.
    rejoin = deepcopy(docs["compose.standby.yml"])
    rejoin["name"] = "nexustix-vm-rejoin"
    rejoin["networks"]["distributed"]["name"] = "nexustix-vm-rejoin-network"
    for key, volume in rejoin["volumes"].items():
        volume["name"] = "nexustix-vm-rejoin_${VM_REJOIN_GENERATION:?Defina generacion NUEVA}_" + key
    for svc in rejoin["services"].values():
        svc["environment"]["NEXUSTIX_PG_UPSTREAM_HOST"] = "${VM_STANDBY_HOST:?Defina nuevo primario}"
        svc["environment"]["NEXUSTIX_PG_SLOT"] = svc["environment"]["POSTGRES_DB"] + "_rejoin"
    (out / "compose.rejoin.yml").write_text("# Reintegración en volúmenes NUEVOS; conservar los antiguos.\n" + yaml.safe_dump(rejoin, sort_keys=False))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only-vm", action="store_true", help="No escribir el Compose del laboratorio")
    generate(parser.parse_args().only_vm)

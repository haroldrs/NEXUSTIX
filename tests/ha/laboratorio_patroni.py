#!/usr/bin/env python3
"""Laboratorio de failover automático: 2 PC + árbitro simulados con Docker.

Genera la configuración con el mismo render que usa el despliegue real
(despliegue-nodos/render.py) y levanta, en una red Docker aislada, tres
votantes etcd y la base `compras_db` administrada por Patroni en pc1 y pc2.

Escenarios medidos:
  1. Arranque: pc1 (principal declarada) inicia el clúster y pc2 replica.
  2. Caída total de la PC principal: se detienen su etcd y su PostgreSQL.
     Se mide cuánto tarda pc2 en ser promovida (RTO) y si se perdió algo (RPO).
  3. Reincorporación: pc1 vuelve y debe quedar como réplica, con los datos nuevos.
  4. Partición de red: la principal queda aislada. Debe pasar a solo lectura y
     la otra PC + el árbitro deben promover a la réplica: nunca dos principales.

Uso: python3 tests/ha/laboratorio_patroni.py --image nexustix-patroni:test
Requiere Docker. Escribe un resumen JSON con los tiempos medidos.
No toca contenedores, redes ni volúmenes fuera del prefijo nxlab-.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "despliegue-nodos"))
import yaml  # noqa: E402
from inventory import validate_inventory  # noqa: E402
from render import ETCD_IMAGE, etcd_cluster, patroni_config, patroni_scope  # noqa: E402

PREFIX = "nxlab"
SUBNET = "10.77.0.0/24"
ADDR = {"pc1": "10.77.0.11", "pc2": "10.77.0.12", "arbitro": "10.77.0.13"}
DOMAIN = "compras"
PASSWORDS = {"admin": "lab-admin-pass", "db": "lab-app-pass", "replication": "lab-repl-pass"}


def inventory():
    nodes = [{"id": nid, "address": ADDR[nid], "platform": "linux", "failure_domain": f"maquina-{nid}",
              "resources": {"cpus": 8, "memory_mb": 8192}, "roles": [] if nid == "arbitro" else
              ["frontend", "gateway", "usuarios", "eventos", "compras"],
              "docker_subnet": f"172.31.{i + 10}.0/24"} for i, nid in enumerate(ADDR)]
    dbs = {d: {"primary": "pc1", "standbys": ["pc2"], "replication": "sync", "sync_quorum": 1}
           for d in ("usuarios", "eventos", "compras")}
    return validate_inventory({"version": 1, "deployment": "lab-ha", "profile": "distribuido",
                               "gateway": {"preferred": "pc1"}, "nodes": nodes, "databases": dbs,
                               "coordination": {"mode": "patroni", "voters": ["pc1", "pc2", "arbitro"]}})


def sh(*args, check=True, capture=True):
    result = subprocess.run(["docker", *args], text=True, capture_output=capture)
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip() if capture else ""


def name(kind, node):
    return f"{PREFIX}-{kind}-{node}"


def cleanup():
    ids = sh("ps", "-aq", "--filter", f"name=^{PREFIX}-", check=False).split()
    if ids:
        sh("rm", "-f", *ids, check=False)
    vols = sh("volume", "ls", "-q", "--filter", f"name=^{PREFIX}-", check=False).split()
    if vols:
        sh("volume", "rm", *vols, check=False)
    sh("network", "rm", f"{PREFIX}-net", check=False)


def start_etcd(inv, node):
    client, peer = inv["ports"]["etcd_client"], inv["ports"]["etcd_peer"]
    sh("run", "-d", "--name", name("etcd", node), "--network", f"{PREFIX}-net", "--ip", ADDR[node],
       "-v", f"{PREFIX}-etcd-{node}:/etcd-data", ETCD_IMAGE, "etcd", f"--name={node}", "--data-dir=/etcd-data",
       f"--listen-client-urls=http://0.0.0.0:{client}", f"--advertise-client-urls=http://{ADDR[node]}:{client}",
       f"--listen-peer-urls=http://0.0.0.0:{peer}", f"--initial-advertise-peer-urls=http://{ADDR[node]}:{peer}",
       f"--initial-cluster={etcd_cluster(inv)}", f"--initial-cluster-token={patroni_scope(inv, 'etcd')}",
       "--initial-cluster-state=new", "--heartbeat-interval=250", "--election-timeout=2500")


def start_patroni(inv, node, image, workdir):
    """En la red de laboratorio cada "PC" comparte la IP de su etcd: se usa
    `--network container:` para que etcd y Patroni vivan en la misma dirección,
    como en una PC real, y la dirección local "etcd" se reemplaza por esa IP."""
    config = patroni_config(inv, next(n for n in inv["nodes"] if n["id"] == node), DOMAIN)
    config["etcd3"]["hosts"] = [h.replace("etcd:", f"{ADDR[node]}:") for h in config["etcd3"]["hosts"]]
    folder = workdir / node
    (folder / "secrets").mkdir(parents=True, exist_ok=True)
    (folder / "patroni.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    for kind, value in PASSWORDS.items():
        (folder / "secrets" / f"{DOMAIN}_{kind}_password").write_text(value + "\n")
    rest = inv["ports"][f"{DOMAIN}_patroni"]
    primary = next(n for n in inv["nodes"] if n["id"] == inv["databases"][DOMAIN]["primary"])
    sh("run", "-d", "--name", name("pg", node), "--network", f"container:{name('etcd', node)}",
       "-v", f"{PREFIX}-pg-{node}:/var/lib/postgresql/data",
       "-v", f"{folder / 'patroni.yml'}:/etc/nexustix/patroni.yml:ro",
       "-v", f"{folder / 'secrets'}:/run/secrets:ro",
       "-e", f"NEXUSTIX_PG_DATABASE={DOMAIN}_db", "-e", f"NEXUSTIX_PATRONI_PORT={rest}",
       "-e", f"NEXUSTIX_PATRONI_PREFERRED={'1' if node == primary['id'] else '0'}",
       "-e", f"NEXUSTIX_PATRONI_PREFERRED_URL=http://{primary['address']}:{rest}",
       "-e", f"NEXUSTIX_PG_ADMIN_PASSWORD_FILE=/run/secrets/{DOMAIN}_admin_password",
       "-e", f"NEXUSTIX_PG_APP_PASSWORD_FILE=/run/secrets/{DOMAIN}_db_password",
       "-e", f"NEXUSTIX_PG_REPLICATION_PASSWORD_FILE=/run/secrets/{DOMAIN}_replication_password",
       image)


def rest_status(inv, node, path):
    """Pregunta a la API de Patroni del nodo desde dentro de su red (sin publicar puertos)."""
    port = inv["ports"][f"{DOMAIN}_patroni"]
    code = sh("exec", name("pg", node), "/usr/local/bin/python3", "-c",
              "import sys,urllib.request\n"
              f"try:\n print(urllib.request.urlopen('http://127.0.0.1:{port}{path}',timeout=2).status)\n"
              "except urllib.error.HTTPError as e: print(e.code)\nexcept Exception: print(0)", check=False)
    return int(code or 0)


def members(inv, node):
    port = inv["ports"][f"{DOMAIN}_patroni"]
    out = sh("exec", name("pg", node), "/usr/local/bin/python3", "-c",
             f"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:{port}/cluster',timeout=2).read().decode())",
             check=False)
    try:
        return {m["name"]: m for m in json.loads(out)["members"]}
    except (ValueError, KeyError):
        return {}


def streaming(inv, observer, member, synchronous=False):
    """Réplica sana: rol de réplica y transmisión WAL activa (no solo creándose).

    En modo síncrono, Patroni solo promueve a la réplica marcada como síncrona:
    es la única con garantía de tener todas las transacciones confirmadas.
    """
    data = members(inv, observer).get(member, {})
    roles = {"sync_standby"} if synchronous else {"replica", "sync_standby"}
    return data.get("role") in roles and data.get("state") == "streaming"


def cluster(inv, node):
    port = inv["ports"][f"{DOMAIN}_patroni"]
    out = sh("exec", name("pg", node), "/usr/local/bin/python3", "-c",
             f"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:{port}/cluster',timeout=2).read().decode())",
             check=False)
    try:
        return {m["name"]: m["role"] for m in json.loads(out)["members"]}
    except (ValueError, KeyError):
        return {}


def wait(predicate, timeout, step=0.5):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return round(time.monotonic() - start, 1)
        time.sleep(step)
    return None


def sql(inv, node, statement):
    port = inv["ports"][f"{DOMAIN}_db"]
    return sh("exec", "-e", f"PGPASSWORD={PASSWORDS['db']}", name("pg", node), "psql", "-h", "127.0.0.1", "-p", str(port),
              "-U", "nexustix_app", "-d", f"{DOMAIN}_db", "-qAt", "-c", statement, check=False)


def report(results, item):
    results["escenarios"].append(item)
    print(json.dumps(item, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="nexustix-patroni:test")
    parser.add_argument("--salida", default=None)
    args = parser.parse_args()
    inv = inventory()
    results = {"escenarios": [], "configuracion": {"ttl_s": 30, "loop_wait_s": 10, "votantes": 3}}
    cleanup()
    workdir = Path(tempfile.mkdtemp(prefix="nxlab-"))
    try:
        sh("network", "create", "--subnet", SUBNET, f"{PREFIX}-net")
        for node in ADDR:
            start_etcd(inv, node)
        start_patroni(inv, "pc1", args.image, workdir)
        start_patroni(inv, "pc2", args.image, workdir)
        member = {n: f"nx_{DOMAIN}_{n}" for n in ("pc1", "pc2")}

        boot = wait(lambda: cluster(inv, "pc1").get(member["pc1"]) == "leader"
                    and streaming(inv, "pc1", member["pc2"], synchronous=True), 240, 2)
        sql(inv, "pc1", "CREATE TABLE IF NOT EXISTS ventas(id serial primary key, detalle text)")
        sql(inv, "pc1", "INSERT INTO ventas(detalle) SELECT 'antes-' || g FROM generate_series(1,50) g")
        report(results, {"nombre": "Arranque: pc1 principal, pc2 réplica", "aprobado": boot is not None,
                                      "segundos": boot, "miembros": cluster(inv, "pc1")})

        # 2. Caída total de la PC principal (su etcd y su base).
        confirmed = int(sql(inv, "pc1", "SELECT count(*) FROM ventas") or 0)
        t0 = time.monotonic()
        sh("kill", name("pg", "pc1"))
        sh("kill", name("etcd", "pc1"))
        promoted = wait(lambda: rest_status(inv, "pc2", "/primary") == 200, 120)
        writable = wait(lambda: sql(inv, "pc2", "INSERT INTO ventas(detalle) VALUES ('tras-caida') RETURNING id").isdigit(), 60)
        rto = round(time.monotonic() - t0, 1) if writable is not None else None
        kept = int(sql(inv, "pc2", "SELECT count(*) FROM ventas WHERE detalle LIKE 'antes-%'") or 0)
        report(results, {"nombre": "Caída total de la PC principal", "aprobado": promoted is not None and kept == confirmed,
                                      "promocion_s": promoted, "rto_s": rto, "filas_confirmadas": confirmed,
                                      "filas_conservadas": kept, "rpo_filas_perdidas": confirmed - kept})

        # 3. Reincorporación de pc1 como réplica.
        sh("start", name("etcd", "pc1"))
        sh("start", name("pg", "pc1"))
        rejoined = wait(lambda: streaming(inv, "pc2", member["pc1"], synchronous=True), 180, 2)
        synced = wait(lambda: sql(inv, "pc1", "SELECT count(*) FROM ventas WHERE detalle='tras-caida'") == "1", 60)
        results["filas_tras_caida_en_pc1"] = sql(inv, "pc1", "SELECT count(*) FROM ventas WHERE detalle='tras-caida'")
        report(results, {"nombre": "Reincorporación de pc1 como réplica", "aprobado": rejoined is not None and synced is not None,
                                      "segundos": rejoined, "miembros": cluster(inv, "pc2")})

        # 4. Partición: la principal actual (pc2) queda aislada de la red.
        t0 = time.monotonic()
        sh("network", "disconnect", f"{PREFIX}-net", name("etcd", "pc2"))
        demoted = wait(lambda: rest_status(inv, "pc2", "/primary") != 200, 90)
        other = wait(lambda: rest_status(inv, "pc1", "/primary") == 200, 120)
        both = rest_status(inv, "pc2", "/primary") == 200 and rest_status(inv, "pc1", "/primary") == 200
        isolated_write = sql(inv, "pc2", "INSERT INTO ventas(detalle) VALUES ('aislada') RETURNING id")
        sh("network", "connect", "--ip", ADDR["pc2"], f"{PREFIX}-net", name("etcd", "pc2"))
        back = wait(lambda: streaming(inv, "pc1", member["pc2"]), 180, 2)
        report(results, {"nombre": "Partición: la principal queda aislada", "aprobado": demoted is not None
                                      and other is not None and not both and not isolated_write.isdigit(),
                                      "degradada_a_lectura_s": demoted, "nueva_principal_s": other,
                                      "dos_principales": both, "escritura_en_aislada_rechazada": not isolated_write.isdigit(),
                                      "reincorporada_s": back, "total_s": round(time.monotonic() - t0, 1)})
    finally:
        cleanup()
    results["aprobado"] = all(item["aprobado"] for item in results["escenarios"])
    text = json.dumps(results, ensure_ascii=False, indent=2)
    print(text)
    if args.salida:
        Path(args.salida).write_text(text + "\n")
    return 0 if results["aprobado"] else 1


if __name__ == "__main__":
    sys.exit(main())

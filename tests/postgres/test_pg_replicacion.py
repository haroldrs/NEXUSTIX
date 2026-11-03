"""Replicación física real (primario + standby) sin Docker: mecánica de PostgreSQL y de las APIs.

Opt-in: NEXUSTIX_TEST_PG_BIN=<carpeta con initdb, pg_ctl, pg_basebackup> (Fedora:
paquete postgresql-server; Windows: binarios de EDB) además de
NEXUSTIX_TEST_PG_ADMIN_URL (base auxiliar para Eventos).

Qué verifica con PostgreSQL real:
- el SQL de scripts/db_ha.py (preparar primario, status, sync, promote) tal cual;
- pg_basebackup -R con slot deja un standby que replica y rechaza escrituras;
- la URL multi-host con target_session_attrs=read-write elige el nodo que
  admite escrituras (también si el standby figura primero);
- tras caer el primario y promover el standby, la MISMA instancia de Compras
  (mismo pool de conexiones) vuelve a comprar sin reconfiguración y conserva
  las compras previas.
Qué NO verifica: la orquestación Docker de db_ha.py (docker stop/update/run para
el fencing, compose run para reincorporar); eso es tests/resiliencia/test_09.
Limpieza: detiene ambos servidores y elimina solo sus carpetas temporales.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from pg_helpers import ROOT, load_service, migrate

BIN = os.getenv("NEXUSTIX_TEST_PG_BIN")
pytestmark = pytest.mark.skipif(not (BIN and os.getenv("NEXUSTIX_TEST_PG_ADMIN_URL")),
                                reason="Opt-in: NEXUSTIX_TEST_PG_BIN y NEXUSTIX_TEST_PG_ADMIN_URL")
sys.path.insert(0, str(ROOT / "scripts"))


def tool(name):
    path = Path(BIN) / (name + (".exe" if os.name == "nt" else ""))
    assert path.exists(), path
    return str(path)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def split_sql(sql):
    """Separa sentencias como psql (respeta comillas simples y bloques $$)."""
    statements, current, i, quote, dollar = [], [], 0, False, False
    while i < len(sql):
        if sql.startswith("$$", i) and not quote:
            dollar = not dollar
            current.append("$$")
            i += 2
            continue
        char = sql[i]
        if char == "'" and not dollar:
            quote = not quote
        if char == ";" and not quote and not dollar:
            statements.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    statements.append("".join(current).strip())
    return [s for s in statements if s]


class Cluster:
    def __init__(self, root: Path, name: str):
        self.data = root / name
        self.port = free_port()
        self.log = root / f"{name}.log"

    # Sin tuberías: en Windows el postmaster hereda stdout/stderr de pg_ctl y
    # subprocess esperaría indefinidamente a que se cierren.
    def start(self):
        subprocess.run([tool("pg_ctl"), "-D", str(self.data), "-l", str(self.log), "-w", "-t", "60", "-o",
                        f"-c listen_addresses=127.0.0.1 -c unix_socket_directories={self.data.parent} -c logging_collector=off -p {self.port}", "start"], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)

    def stop(self, mode="fast"):
        subprocess.run([tool("pg_ctl"), "-D", str(self.data), "-m", mode, "-w", "stop"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)

    def url(self, db="postgres"):
        return f"postgresql://postgres@127.0.0.1:{self.port}/{db}"


def psql_emulated(clusters):
    """Sustituye pg_docker.psql: 'container' = nombre de nodo; salida como psql -A -t."""
    import psycopg2

    def run(container, database, sql, **_kwargs):
        connection = psycopg2.connect(host="127.0.0.1", port=clusters[container].port, user="postgres", dbname=database)
        connection.autocommit = True
        output = []
        try:
            with connection.cursor() as cursor:
                for statement in split_sql(sql):
                    cursor.execute(statement)
                    if cursor.description:
                        for row in cursor.fetchall():
                            output.append("|".join("t" if v is True else "f" if v is False else "" if v is None else str(v)
                                                   for v in row))
        finally:
            connection.close()
        return "\n".join(output).strip()
    return run


@pytest.fixture
def pair(tmp_path, monkeypatch, keys):
    import db_ha
    import entornos
    import pg_docker

    root = Path(os.getenv("NEXUSTIX_TEST_PG_TMP", tmp_path)) / f"repl-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    primary, standby = Cluster(root, "compras_db"), Cluster(root, "compras_db_replica")
    subprocess.run([tool("initdb"), "-D", str(primary.data), "-U", "postgres", "-E", "UTF8", "--locale=C",
                    "--auth-local=trust", "--auth-host=trust", "--no-instructions"], check=True, capture_output=True)
    with (primary.data / "postgresql.conf").open("a", encoding="utf-8") as conf:
        conf.write("\nwal_level = replica\nmax_wal_senders = 10\nmax_replication_slots = 10\nhot_standby = on\n"
                   "wal_log_hints = on\nmax_connections = 100\n")
    # Igual que el despliegue: la replicación exige contraseña SCRAM.
    hba = (primary.data / "pg_hba.conf").read_text(encoding="utf-8")
    hba = "host replication replicator 127.0.0.1/32 scram-sha-256\n" + hba
    (primary.data / "pg_hba.conf").write_text(hba, encoding="utf-8")
    primary.start()
    clusters = {"compras_db": primary, "compras_db_replica": standby}
    monkeypatch.setattr(pg_docker, "psql", psql_emulated(clusters))
    secrets_dir = root / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "replication-password").write_text(uuid.uuid4().hex)
    monkeypatch.setenv("NEXUSTIX_STAGING_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setattr(db_ha, "_docker", lambda *a, **k: None)  # la regla pg_hba ya está escrita
    entorno = entornos.get("pruebas")
    yield {"root": root, "primary": primary, "standby": standby, "clusters": clusters, "entorno": entorno,
           "db_ha": db_ha, "pg_docker": pg_docker, "password": (secrets_dir / "replication-password").read_text()}
    for cluster in (standby, primary):
        cluster.stop("immediate")
    shutil.rmtree(root, ignore_errors=True)


def node(pg_docker, service, running=True, standby=False):
    return pg_docker.Nodo(service, service, running, standby if running else None)


def test_replica_promotion_and_api_failover_with_real_postgres(pair, monkeypatch, pg_url_factory):
    db_ha, pg_docker, entorno = pair["db_ha"], pair["pg_docker"], pair["entorno"]
    primary, standby = pair["primary"], pair["standby"]

    # 1) SQL real de db_ha: rol de replicación, recarga y slot (idempotente: dos veces).
    subprocess.run([tool("psql"), "-h", "127.0.0.1", "-p", str(primary.port), "-U", "postgres", "-d", "postgres",
                    "-c", "CREATE DATABASE compras_db"], check=True, capture_output=True)
    for _ in range(2):
        db_ha.prepare_primary(entorno, "compras_db", node(pg_docker, "compras_db"), "compras_db_replica")

    # 2) Standby con pg_basebackup -R usando el slot y la contraseña SCRAM.
    env = {**os.environ, "PGPASSWORD": pair["password"]}
    subprocess.run([tool("pg_basebackup"), "-h", "127.0.0.1", "-p", str(primary.port), "-U", "replicator",
                    "-D", str(standby.data), "-R", "-X", "stream", "--checkpoint=fast", "--slot=compras_db_replica"],
                   check=True, capture_output=True, env=env)
    standby.start()
    assert pg_docker.psql("compras_db_replica", "compras_db", "SELECT pg_is_in_recovery();") == "t"

    # 3) Compras con la URL de producción (multi-host + read-write), primario primero.
    ha_url = (f"postgresql://postgres@/compras_db?host=127.0.0.1:{primary.port}&host=127.0.0.1:{standby.port}"
              "&target_session_attrs=read-write")
    assert migrate("compras", ha_url, monkeypatch) == 0
    eventos_url = pg_url_factory()
    ev_main, ev_models, ev_db = load_service("eventos", eventos_url, monkeypatch)
    ev_main.esperar_bd()
    with ev_db.SessionLocal() as db:
        db.add(ev_models.Evento(id=900002, nombre="Sintético HA", stock=10, stock_inicial=10))
        db.commit()
    from test_pg_integracion import EventosEnProceso, buy, buyer
    co_main, co_models, co_db = load_service("compras", ha_url, monkeypatch)
    adapter = EventosEnProceso(ev_main, ev_db.SessionLocal)
    monkeypatch.setattr(co_main.requests, "post", adapter.post)
    monkeypatch.setattr(co_main.requests, "get", adapter.get)
    saga = {"co": co_main, "co_db": co_db}
    principal = buyer(co_main, 700)
    status, first = buy(saga, f"ha-1-{uuid.uuid4().hex}", principal, event_id=900002)
    assert status == 200

    # 4) La compra llega al standby, que rechaza escrituras.
    deadline = time.monotonic() + 20
    while pg_docker.psql("compras_db_replica", "compras_db",
                         f"SELECT count(*) FROM compras WHERE id = {first['id']};") != "1":
        assert time.monotonic() < deadline, "la compra no se replicó"
        time.sleep(0.3)
    with pytest.raises(Exception, match="read-only"):
        pg_docker.psql("compras_db_replica", "compras_db", "INSERT INTO journal_compras (reloj_t, instancia, tipo, mensaje) VALUES (0, 'x', 'x', 'x');")
    # target_session_attrs=read-write salta un standby aunque figure primero en la lista.
    import psycopg2
    connection = psycopg2.connect(host="127.0.0.1,127.0.0.1", port=f"{standby.port},{primary.port}",
                                  user="postgres", dbname="compras_db", target_session_attrs="read-write",
                                  connect_timeout=3)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_is_in_recovery(), inet_server_port()")
            assert cursor.fetchone() == (False, primary.port)
    finally:
        connection.close()

    # 5) status y sync de db_ha sobre los nodos reales.
    monkeypatch.setattr(pg_docker, "nodos", lambda _e, base: [node(pg_docker, "compras_db"),
                                                               node(pg_docker, "compras_db_replica", standby=True)]
                        if base == "compras_db" else [])
    report = db_ha.status(entorno)["compras_db"]
    assert report["veredicto"] == "REPLICADO"
    assert report["nodos"][0]["standbys"][0]["estado"] == "streaming"
    db_ha.sync(entorno, "compras_db", True)
    assert pg_docker.psql("compras_db", "compras_db", "SHOW synchronous_standby_names;") == "ANY 1 (*)"
    status, _ = buy(saga, f"ha-sync-{uuid.uuid4().hex}", principal, event_id=900002)
    assert status == 200, "con el standby vivo la replicación síncrona no bloquea"

    # 6) Cae el primario (parada inmediata = caída) y db_ha.promote promueve el standby.
    primary.stop("immediate")
    fenced = []
    monkeypatch.setattr(pg_docker, "nodos", lambda _e, base: [node(pg_docker, "compras_db", running=False),
                                                               node(pg_docker, "compras_db_replica", standby=True)]
                        if base == "compras_db" else [])
    monkeypatch.setattr(db_ha, "_fence", lambda _e, _b, nodo, _m: fenced.append(nodo.servicio))
    started = time.monotonic()
    result = db_ha.promote(entorno, "compras_db")
    assert fenced == ["compras_db"] and result["nuevo_primario"] == "compras_db_replica"
    assert pg_docker.psql("compras_db_replica", "compras_db", "SHOW synchronous_standby_names;") == "", \
        "el nuevo primario no debe esperar a un standby inexistente"

    # 7) La MISMA instancia de Compras (mismo pool) compra en el nuevo primario.
    key = f"ha-2-{uuid.uuid4().hex}"
    deadline = time.monotonic() + 30
    while True:
        try:
            status, second = buy(saga, key, principal, event_id=900002)
            if status == 200:
                break
        except Exception:  # noqa: BLE001 - conexión vieja descartada; se reintenta la misma clave
            pass
        assert time.monotonic() < deadline, "Compras no se reconectó al nuevo primario"
        time.sleep(0.5)
    print(f"\nPromoción -> primera compra correcta: {time.monotonic() - started:.1f} s")
    rows = pg_docker.psql("compras_db_replica", "compras_db", "SELECT count(*) FROM compras;")
    assert rows == "3", "las compras previas a la promoción se conservan"
    slots = pg_docker.psql("compras_db_replica", "compras_db", "SELECT slot_name FROM pg_replication_slots;")
    assert "compras_db" in slots.split(), "slot listo para reincorporar el nodo antiguo"

    # 8) Reintegración real: conservar el directorio antiguo y clonar uno NUEVO.
    # Esta prueba solo opera sobre los clústeres temporales de la fixture.
    archived = primary.data.with_name("compras_db_before_rejoin")
    primary.data.rename(archived)
    subprocess.run([tool("pg_basebackup"), "-h", "127.0.0.1", "-p", str(standby.port), "-U", "replicator",
                    "-D", str(primary.data), "-R", "-X", "stream", "--checkpoint=fast", "--slot=compras_db"],
                   check=True, capture_output=True, env=env)
    primary.start()
    assert archived.is_dir()
    assert pg_docker.psql("compras_db", "compras_db", "SELECT pg_is_in_recovery();") == "t"
    assert pg_docker.psql("compras_db", "compras_db", "SELECT count(*) FROM compras;") == "3"
    with pytest.raises(Exception, match="read-only"):
        pg_docker.psql("compras_db", "compras_db", "CREATE TABLE must_not_write (id int);")

    co_db.engine.dispose()
    ev_db.engine.dispose()

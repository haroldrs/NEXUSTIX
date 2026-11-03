#!/usr/bin/env python3
"""Replicación y conmutación controlada de PostgreSQL para NEXUSTIX.

    python3 scripts/db_ha.py pruebas enable                 # prepara primarios y crea los standby
    python3 scripts/db_ha.py pruebas status                 # roles, lag y slots de cada base
    python3 scripts/db_ha.py pruebas promote compras_db     # failover controlado (con fencing)
    python3 scripts/db_ha.py pruebas rejoin compras_db compras_db
    python3 scripts/db_ha.py pruebas switchback compras_db  # vuelve al nodo original
    python3 scripts/db_ha.py pruebas sync compras_db on|off
    python3 scripts/db_ha.py pruebas disable                # retira los standby (conserva volúmenes)

Protocolo de promoción (evita dos primarios):
 1. Verifica que el standby está en recuperación y recibe WAL.
 2. Aísla al primario actual: lo detiene, desactiva su reinicio automático y
    escribe NEXUSTIX_FENCED en su volumen; el entrypoint se niega a arrancarlo.
    Si el primario está en otra máquina inaccesible, exige
    --primario-aislado-manualmente (el operador garantiza que está apagado).
 3. Promueve el standby (pg_promote) y comprueba que acepta escrituras.
 4. Los servicios ya usan URL multi-host con target_session_attrs=read-write y
    pool_pre_ping: las conexiones nuevas llegan al nuevo primario.
No hay failover automático: con dos nodos no existe quórum que distinga una
caída real de una partición de red.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entornos  # noqa: E402
import pg_docker  # noqa: E402
from pg_docker import PgError  # noqa: E402

PGDATA = "/var/lib/postgresql/data"
REPLICATION_USER = "replicator"


def _compose(entorno: entornos.Entorno, *args: str, timeout: int = 600):
    result = entornos.run([*entorno.compose_args(ha=True), *args], env=entorno.env(), timeout=timeout)
    if result.returncode:
        raise PgError(f"docker compose {' '.join(args[:3])} falló: {(result.stderr or '').strip()[-600:]}")
    return result


def _docker(*args: str, timeout: int = 120):
    result = entornos.run(["docker", *args], timeout=timeout)
    if result.returncode:
        raise PgError(f"docker {' '.join(args[:2])} falló: {(result.stderr or '').strip()[-400:]}")
    return result


def _other(base: str, servicio: str) -> str:
    return f"{base}_replica" if servicio == base else base


def _replication_password(entorno: entornos.Entorno) -> str:
    path = entorno.secrets_dir / "replication-password"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise PgError(f"No se pudo leer {path}; genere los secretos del entorno") from None
    if not value or "'" in value or "\\" in value:
        raise PgError("Contraseña de replicación vacía o con caracteres no admitidos")
    return value


def prepare_primary(entorno: entornos.Entorno, base: str, nodo: pg_docker.Nodo, slot: str) -> None:
    """Idempotente: rol de replicación, regla pg_hba, recarga y slot físico."""
    password = _replication_password(entorno)
    pg_docker.psql(nodo.container, base, f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{REPLICATION_USER}') THEN
    CREATE ROLE {REPLICATION_USER} WITH REPLICATION LOGIN PASSWORD '{password}';
  ELSE
    ALTER ROLE {REPLICATION_USER} WITH REPLICATION LOGIN PASSWORD '{password}';
  END IF;
END $$;
""")
    rule = f"host replication {REPLICATION_USER} all scram-sha-256"
    _docker("exec", "-u", "postgres", nodo.container, "sh", "-c",
            f"grep -qxF '{rule}' {PGDATA}/pg_hba.conf || echo '{rule}' >> {PGDATA}/pg_hba.conf")
    pg_docker.psql(nodo.container, base, "SELECT pg_reload_conf();")
    pg_docker.psql(nodo.container, base, f"""
SELECT pg_create_physical_replication_slot('{slot}')
WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '{slot}');
""")


def status(entorno: entornos.Entorno) -> dict:
    report = {}
    for base in entornos.DATABASES:
        info = {"nodos": []}
        for nodo in pg_docker.nodos(entorno, base):
            item = {"servicio": nodo.servicio, "en_ejecucion": nodo.running,
                    "rol": {True: "standby", False: "primario", None: "desconocido"}[nodo.en_recuperacion]}
            if nodo.running and nodo.en_recuperacion:
                item["replicacion"] = pg_docker.psql_json(nodo.container, base, """
SELECT json_build_object(
  'recibido_lsn', pg_last_wal_receive_lsn()::text,
  'aplicado_lsn', pg_last_wal_replay_lsn()::text,
  'retraso_segundos', COALESCE(EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp()), 0),
  'receptor', (SELECT status FROM pg_stat_wal_receiver LIMIT 1)
)::text;""")
            if nodo.running and nodo.en_recuperacion is False:
                item["standbys"] = pg_docker.psql_json(nodo.container, base, """
SELECT COALESCE(json_agg(json_build_object(
  'application_name', application_name, 'estado', state, 'sync_state', sync_state,
  'retraso_replay_bytes', pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn))), '[]'::json)::text
FROM pg_stat_replication;""")
                item["slots"] = pg_docker.psql_json(nodo.container, base, """
SELECT COALESCE(json_agg(json_build_object('slot', slot_name, 'activo', active,
  'wal_retenido_bytes', pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))), '[]'::json)::text
FROM pg_replication_slots;""")
                item["synchronous_standby_names"] = pg_docker.psql(nodo.container, base, "SHOW synchronous_standby_names;")
            info["nodos"].append(item)
        primarios = [n for n in info["nodos"] if n["rol"] == "primario"]
        info["veredicto"] = ("DOS_PRIMARIOS" if len(primarios) > 1 else
                             "SIN_PRIMARIO" if not primarios else
                             "REPLICADO" if any(n["rol"] == "standby" for n in info["nodos"]) else
                             "SIN_STANDBY")
        report[base] = info
    return report


def enable(entorno: entornos.Entorno) -> None:
    # Recrea los primarios con los parámetros de replicación (reinicio breve)
    # y el resto de servicios con la URL multi-host.
    print("Aplicando overlay de replicación (los primarios se reinician una vez)...")
    _compose(entorno, "up", "-d", "--build", "--wait", "--wait-timeout", "300",
             *entornos.DATABASES, timeout=1200)
    for base in entornos.DATABASES:
        nodo = pg_docker.primario(entorno, base)
        prepare_primary(entorno, base, nodo, f"{base}_replica")
        print(f"  {base}: primario preparado en {nodo.servicio}")
    _compose(entorno, "up", "-d", "--build", "--wait", "--wait-timeout", "600", timeout=1800)
    print("Réplicas en marcha. Revise: python3 scripts/db_ha.py", entorno.nombre, "status")


def disable(entorno: entornos.Entorno) -> None:
    """Vuelve a la topología de un nodo por base, solo desde el estado normal.

    Retira los contenedores *_db_replica (sus volúmenes se conservan), elimina
    los slots de replicación (un slot inactivo retendría WAL sin límite) y
    recrea los servicios con la URL de un solo nodo.
    """
    for base in entornos.DATABASES:
        current = pg_docker.primario(entorno, base)
        if current.servicio != base:
            raise PgError(f"{base}: el primario es {current.servicio}; ejecute antes 'switchback {base}'")
    for base in entornos.DATABASES:
        current = pg_docker.primario(entorno, base)
        pg_docker.psql(current.container, base, f"""
SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots
WHERE slot_name = '{base}_replica' AND NOT active;""")
    replicas = [f"{base}_replica" for base in entornos.DATABASES]
    _compose(entorno, "stop", *replicas, timeout=300)
    _compose(entorno, "rm", "-f", *replicas, timeout=300)
    plain = entornos.run([*entorno.compose_args(ha=False), "up", "-d", "--wait", "--wait-timeout", "300"],
                         env=entorno.env(), timeout=1200)
    if plain.returncode:
        raise PgError(f"No se pudo recrear la topología sin réplicas: {(plain.stderr or '').strip()[-600:]}")
    for base in entornos.DATABASES:
        current = pg_docker.primario(entorno, base)
        pg_docker.psql(current.container, base, f"""
SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots
WHERE slot_name = '{base}_replica' AND NOT active;""")
    print("Replicación deshabilitada; volúmenes *_replica_data conservados (no se borró ningún dato).")


def _fence(entorno: entornos.Entorno, base: str, nodo: pg_docker.Nodo, motivo: str) -> None:
    entornos.verify_labels(entorno, nodo.container, nodo.servicio)
    if nodo.running:
        _docker("stop", "--time", "30", nodo.container, timeout=90)
    # Un contenedor aislado no debe revivir por su política de reinicio.
    _docker("update", "--restart=no", nodo.container)
    image = entornos.inspect(nodo.container, "{{.Config.Image}}")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    note = f"aislado={stamp} motivo={motivo} nuevo_primario={_other(base, nodo.servicio)}"
    _docker("run", "--rm", "--volumes-from", nodo.container, "--entrypoint", "sh", image, "-c",
            f"printf '%s\\n' '{note}' > {PGDATA}/NEXUSTIX_FENCED", timeout=120)


def promote(entorno: entornos.Entorno, base: str, primario_aislado_manualmente: bool = False,
            motivo: str = "promocion_controlada") -> dict:
    nodos = {n.servicio: n for n in pg_docker.nodos(entorno, base)}
    standbys = [n for n in nodos.values() if n.running and n.en_recuperacion]
    primarios = [n for n in nodos.values() if n.running and n.en_recuperacion is False]
    if len(standbys) != 1:
        raise PgError(f"{base}: se necesita exactamente un standby en ejecución; hay {len(standbys)}")
    standby = standbys[0]
    if len(primarios) > 1:
        raise pg_docker.DosPrimariosError(f"{base}: ya hay dos primarios; no se promueve nada")
    old_service = _other(base, standby.servicio)
    old = nodos.get(old_service)
    lag = pg_docker.psql_json(standby.container, base, """
SELECT json_build_object('recibido', pg_last_wal_receive_lsn()::text, 'aplicado', pg_last_wal_replay_lsn()::text,
  'pendiente_bytes', pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()))::text;""")
    if old is None:
        if not primario_aislado_manualmente:
            raise PgError(f"No se encontró el contenedor de {old_service}. Si el primario está en otra máquina, "
                          "apáguelo o aíslelo y repita con --primario-aislado-manualmente")
    else:
        _fence(entorno, base, old, motivo)
    print(f"{base}: {old_service} aislado; promoviendo {standby.servicio}...")
    pg_docker.psql(standby.container, base, "SELECT pg_promote(true, 60);", timeout=90)
    deadline = time.monotonic() + 60
    while pg_docker.psql(standby.container, base, "SELECT pg_is_in_recovery();") != "f":
        if time.monotonic() > deadline:
            raise PgError(f"{standby.servicio} no salió de recuperación tras pg_promote")
        time.sleep(1)
    # El nodo aislado se reincorporará como standby de este: dejar su slot listo.
    pg_docker.psql(standby.container, base, f"""
SELECT pg_create_physical_replication_slot('{old_service}')
WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '{old_service}');""")
    # Con el standby convertido en primario no hay a quién esperar.
    pg_docker.psql(standby.container, base, "ALTER SYSTEM RESET synchronous_standby_names; SELECT pg_reload_conf();")
    result = {"base": base, "nuevo_primario": standby.servicio, "aislado": old_service,
              "wal_antes_de_promover": lag, "promovido_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def rejoin(entorno: entornos.Entorno, base: str, servicio: str, archive_dir: Path) -> None:
    """Reconstruye un nodo aislado como standby del primario actual (archiva antes su volumen)."""
    if servicio not in (base, f"{base}_replica"):
        raise PgError(f"{servicio} no es un nodo de {base}")
    current = pg_docker.primario(entorno, base)
    if current.servicio == servicio:
        raise PgError(f"{servicio} es el primario actual; no se reconstruye")
    container = entornos.container_id(entorno, servicio)
    entornos.verify_labels(entorno, container, servicio)
    if pg_docker.is_running(container):
        _docker("stop", "--time", "30", container, timeout=90)
    archive_dir = archive_dir.expanduser().resolve()
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"{entorno.proyecto}_{servicio}_{stamp}.tar.gz"
    image = entornos.inspect(container, "{{.Config.Image}}")
    # 1) Archivar el directorio de datos completo (nada se pierde sin copia).
    _docker("run", "--rm", "--volumes-from", container, "-v", f"{archive_dir}:/archivo:z",
            "--entrypoint", "sh", image, "-c", f"tar czf /archivo/{archive_name} -C {PGDATA} .", timeout=3600)
    # 2) Vaciar el volumen y clonar desde el primario actual.
    _docker("run", "--rm", "--volumes-from", container, "--entrypoint", "sh", image, "-c",
            f"find {PGDATA} -mindepth 1 -delete", timeout=600)
    prepare_primary(entorno, base, current, servicio)
    _compose(entorno, "run", "--rm", "--no-deps",
             "-e", f"NEXUSTIX_PG_UPSTREAM_HOST={current.servicio}",
             "-e", f"NEXUSTIX_PG_SLOT={servicio}",
             servicio, "bootstrap-standby", timeout=3600)
    _docker("update", "--restart=unless-stopped", container)
    _compose(entorno, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "300", servicio, timeout=900)
    nodo = next(n for n in pg_docker.nodos(entorno, base) if n.servicio == servicio)
    if not nodo.en_recuperacion:
        raise PgError(f"{servicio} arrancó pero no está en modo standby")
    print(f"{base}: {servicio} reincorporado como standby de {current.servicio}. Copia previa: {archive_dir / archive_name}")


def switchback(entorno: entornos.Entorno, base: str, archive_dir: Path) -> None:
    """Vuelve al estado normal: <base> primario y <base>_replica standby."""
    current = pg_docker.primario(entorno, base)
    if current.servicio == base:
        print(f"{base}: el nodo original ya es primario; nada que hacer.")
        return
    standby = next((n for n in pg_docker.nodos(entorno, base) if n.servicio == base), None)
    if standby is None or not standby.running or not standby.en_recuperacion:
        raise PgError(f"Reincorpore primero {base} como standby: db_ha.py {entorno.nombre} rejoin {base} {base}")
    # Conmutación planificada: esperar a que el standby haya aplicado todo el WAL.
    target = pg_docker.psql(current.container, base, "SELECT pg_current_wal_lsn()::text;")
    deadline = time.monotonic() + 60
    while pg_docker.psql(standby.container, base,
                         f"SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '{target}') >= 0;") != "t":
        if time.monotonic() > deadline:
            raise PgError("El standby no alcanzó al primario; no se conmuta")
        time.sleep(1)
    promote(entorno, base, motivo="conmutacion_de_retorno")
    rejoin(entorno, base, f"{base}_replica", archive_dir)


def sync(entorno: entornos.Entorno, base: str, enabled: bool) -> None:
    current = pg_docker.primario(entorno, base)
    value = "ANY 1 (*)" if enabled else ""
    pg_docker.psql(current.container, base,
                   f"ALTER SYSTEM SET synchronous_standby_names = '{value}'; SELECT pg_reload_conf();")
    print(f"{base}: replicación {'síncrona (ANY 1)' if enabled else 'asíncrona'} en {current.servicio}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("entorno", choices=sorted(entornos.ENTORNOS))
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("base", choices=entornos.DATABASES)
    p_promote.add_argument("--primario-aislado-manualmente", action="store_true",
                           help="El primario está en otra máquina y el operador garantiza que está apagado/aislado")
    p_rejoin = sub.add_parser("rejoin")
    p_rejoin.add_argument("base", choices=entornos.DATABASES)
    p_rejoin.add_argument("servicio")
    p_rejoin.add_argument("--archivo", type=Path, default=Path("~/nexustix-archivo-nodos"))
    p_switch = sub.add_parser("switchback")
    p_switch.add_argument("base", choices=entornos.DATABASES)
    p_switch.add_argument("--archivo", type=Path, default=Path("~/nexustix-archivo-nodos"))
    p_sync = sub.add_parser("sync")
    p_sync.add_argument("base", choices=entornos.DATABASES)
    p_sync.add_argument("modo", choices=["on", "off"])
    args = parser.parse_args(argv)
    entorno = entornos.get(args.entorno)
    try:
        if args.comando == "status":
            print(json.dumps(status(entorno), ensure_ascii=False, indent=2))
        elif args.comando == "enable":
            enable(entorno)
        elif args.comando == "disable":
            disable(entorno)
        elif args.comando == "promote":
            promote(entorno, args.base, args.primario_aislado_manualmente)
        elif args.comando == "rejoin":
            rejoin(entorno, args.base, args.servicio, args.archivo)
        elif args.comando == "switchback":
            switchback(entorno, args.base, args.archivo)
        elif args.comando == "sync":
            sync(entorno, args.base, args.modo == "on")
        return 0
    except (PgError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

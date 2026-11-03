#!/usr/bin/env python3
"""Create, verify and restore logical backups of the three NEXUSTIX databases.

    backup   --entorno E --compose-project P --output-dir D --confirm-writes-paused
    verify   --backup-dir D [--rehearse-migrations]
    restore  --entorno E --compose-project P --backup-dir D --confirm-empty-target

The target environment is always explicit: --entorno selects demo or pruebas
and --compose-project must repeat its exact Compose project name. Containers
are located by Compose labels (project + service), never by fixed names, and
with replication enabled the dump is taken from the node that is primary.

``verify`` deliberately has no Compose configuration or secret dependency: it
restores the archives into a new internal-only Docker network and a tmpfs
PostgreSQL, then compares business aggregates, per-table row counts and the
schema fingerprint (tables, columns, constraints, foreign keys, indexes and
Alembic revision) recorded at backup time.

``restore`` is the disaster-recovery path. It only writes into databases that
contain no tables (fresh volumes), so it can never overwrite existing data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entornos  # noqa: E402

ROOT = entornos.ROOT
FORMAT_VERSION = 1
DATABASES = {
    "usuarios_db": {"service": "usuarios_db"},
    "eventos_db": {"service": "eventos_db"},
    "compras_db": {"service": "compras_db"},
}
# Tablas cuyo número de filas cambia con la actividad normal de autenticación
# (refresh rotatorio); se registran pero no bloquean la comparación.
VOLATILE_TABLES = {"refresh_sessions"}

SCHEMA_SQL = """
SELECT json_build_object(
  'tables', (SELECT coalesce(json_agg(table_name ORDER BY table_name), '[]'::json)
             FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'),
  'columns', (SELECT coalesce(json_agg(table_name || '.' || column_name || ':' || data_type || ':' || is_nullable
                                       ORDER BY table_name, column_name), '[]'::json)
              FROM information_schema.columns WHERE table_schema = 'public'),
  'constraints', (SELECT coalesce(json_agg(c.conrelid::regclass::text || '.' || c.conname || ':' || c.contype::text
                                           ORDER BY c.conrelid::regclass::text, c.conname), '[]'::json)
                  FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public'),
  'foreign_keys', (SELECT coalesce(json_agg(c.conrelid::regclass::text || '->' || c.confrelid::regclass::text || ':' || c.conname
                                            ORDER BY c.conname), '[]'::json)
                   FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname = 'public' AND c.contype = 'f'),
  'indexes', (SELECT coalesce(json_agg(tablename || '.' || indexname ORDER BY tablename, indexname), '[]'::json)
              FROM pg_indexes WHERE schemaname = 'public'),
  'alembic_revision', (CASE WHEN to_regclass('public.alembic_version') IS NULL THEN NULL
                       ELSE (xpath('/row/v/text()', query_to_xml('SELECT version_num AS v FROM public.alembic_version', false, true, '')))[1]::text END),
  'row_counts', (SELECT coalesce(json_object_agg(table_name,
                   (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM public.%I', table_name), false, true, '')))[1]::text::bigint
                   ORDER BY table_name), '{}'::json)
                 FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE')
)::text
"""

STATS_SQL = {
    "usuarios_db": """
        SELECT json_build_object(
          'usuarios_total', (SELECT count(*) FROM usuarios)
        )::text
    """,
    "eventos_db": """
        SELECT json_build_object(
          'eventos_total', (SELECT count(*) FROM eventos),
          'stock_total', (SELECT coalesce(sum(stock), 0) FROM eventos),
          'stock_por_evento', (SELECT coalesce(json_agg(json_build_object('id', id, 'stock', stock) ORDER BY id), '[]'::json) FROM eventos),
          'reservas_total', (SELECT count(*) FROM reservas_stock),
          'reservas_por_estado_evento', (
            SELECT coalesce(json_agg(json_build_object('evento_id', evento_id, 'estado', estado, 'operaciones', operaciones, 'cantidad', cantidad)
                                     ORDER BY evento_id, estado), '[]'::json)
            FROM (SELECT evento_id, estado, count(*) AS operaciones, coalesce(sum(cantidad), 0) AS cantidad
                  FROM reservas_stock GROUP BY evento_id, estado) s
          )
        )::text
    """,
    "compras_db": """
        SELECT json_build_object(
          'compras_total', (SELECT count(*) FROM compras),
          'compras_por_estado', (
            SELECT coalesce(json_agg(json_build_object('estado', estado, 'operaciones', operaciones, 'cantidad', cantidad)
                                     ORDER BY estado), '[]'::json)
            FROM (SELECT estado, count(*) AS operaciones, coalesce(sum(cantidad), 0) AS cantidad
                  FROM compras GROUP BY estado) s
          ),
          'compras_confirmadas', (SELECT count(*) FROM compras WHERE estado = 'confirmada'),
          'cantidad_confirmada', (SELECT coalesce(sum(cantidad), 0) FROM compras WHERE estado = 'confirmada'),
          'compras_confirmadas_por_evento', (
            SELECT coalesce(json_agg(json_build_object('evento_id', evento_id, 'operaciones', operaciones, 'cantidad', cantidad)
                                     ORDER BY evento_id), '[]'::json)
            FROM (SELECT evento_id, count(*) AS operaciones, coalesce(sum(cantidad), 0) AS cantidad
                  FROM compras WHERE estado = 'confirmada' GROUP BY evento_id) s
          ),
          'journal_total', (SELECT count(*) FROM journal_compras),
          'journal_por_tipo', (
            SELECT coalesce(json_agg(json_build_object('tipo', tipo, 'registros', registros) ORDER BY tipo), '[]'::json)
            FROM (SELECT tipo, count(*) AS registros FROM journal_compras GROUP BY tipo) s
          )
        )::text
    """,
}


class ProcedureError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact(text: str) -> str:
    text = re.sub(r"(?i)(password|token|secret|authorization)(\s*[=:]\s*)\S+", r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@/\s]+@", r"\1[REDACTED]@", text)
    return text.strip()


def run_checked(args: list[str], *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
                timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(args, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcedureError(f"No se pudo ejecutar {shlex.join(args[:4])}: {_redact(str(exc))}") from exc
    if result.returncode:
        detail = _redact((result.stderr or b"").decode("utf-8", "replace"))
        raise ProcedureError(
            f"Falló {shlex.join(args[:5])} (código {result.returncode})"
            + (f": {detail[-1800:]}" if detail else "")
        )
    return result


def _docker_label(container: str, label: str) -> str:
    result = run_checked(["docker", "inspect", "--format", f"{{{{ index .Config.Labels \"{label}\" }}}}", container])
    return result.stdout.decode().strip()


def resolve_primary_container(entorno: entornos.Entorno, database: str) -> tuple[str, str]:
    """(container_id, service) of the node of ``database`` that accepts writes.

    Candidates are found only through Compose labels of the exact project, and
    the labels are re-read before use. Two writable nodes abort the backup.
    """
    writable = []
    for service in (database, f"{database}_replica"):
        listed = run_checked([
            "docker", "ps", "--no-trunc", "--format", "{{.ID}}",
            "--filter", f"label=com.docker.compose.project={entorno.proyecto}",
            "--filter", f"label=com.docker.compose.service={service}",
            "--filter", "label=com.docker.compose.oneoff=False",
        ]).stdout.decode().split()
        for container in listed:
            project_label = _docker_label(container, "com.docker.compose.project")
            service_label = _docker_label(container, "com.docker.compose.service")
            if project_label != entorno.proyecto or service_label != service:
                raise ProcedureError(
                    f"Destino rechazado para {database}: etiquetas Compose no coinciden "
                    f"(proyecto/servicio esperado {entorno.proyecto}/{service})"
                )
            if _psql(container, database, "SELECT pg_is_in_recovery()") == "f":
                writable.append((container, service))
    if len(writable) > 1:
        raise ProcedureError(f"{database}: hay dos nodos que aceptan escrituras; no se respalda")
    if not writable:
        raise ProcedureError(f"{database}: no hay un nodo primario en ejecución en el proyecto {entorno.proyecto}")
    return writable[0]


def _psql(container: str, database: str, sql: str) -> str:
    result = run_checked([
        "docker", "exec", "-u", "postgres", container,
        "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", database, "-c", sql,
    ])
    return result.stdout.decode("utf-8").strip()


def _read_database_snapshot(name: str, container: str) -> dict[str, Any]:
    version = _psql(container, name, "SELECT json_build_object('version', current_setting('server_version'), 'version_num', current_setting('server_version_num'))::text")
    stats = _psql(container, name, STATS_SQL[name])
    schema = _psql(container, name, SCHEMA_SQL)
    try:
        return {"postgres": json.loads(version), "stats": json.loads(stats), "schema": json.loads(schema)}
    except json.JSONDecodeError as exc:
        raise ProcedureError(f"La consulta de metadatos de {name} no devolvió JSON válido") from exc


def compare_schema(expected: dict[str, Any], actual: dict[str, Any], database: str) -> None:
    """Structure must match exactly; row counts too, except volatile auth tables."""
    for key in ("tables", "columns", "constraints", "foreign_keys", "indexes", "alembic_revision"):
        if expected.get(key) != actual.get(key):
            raise ProcedureError(f"El esquema restaurado de {database} difiere en '{key}'")
    expected_counts = {k: v for k, v in (expected.get("row_counts") or {}).items() if k not in VOLATILE_TABLES}
    actual_counts = {k: v for k, v in (actual.get("row_counts") or {}).items() if k not in VOLATILE_TABLES}
    if expected_counts != actual_counts:
        raise ProcedureError(f"El número de filas por tabla restaurado de {database} no coincide")


def postgres_major(snapshot: dict[str, Any]) -> int:
    version_num = int(snapshot["postgres"]["version_num"])
    return version_num // 10000 if version_num >= 100000 else version_num // 10000


def write_json_private(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(path.name + ".tmp")
    encoded = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    os.chmod(path, 0o600)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_same_stats(expected: dict[str, Any], actual: dict[str, Any], database: str) -> None:
    if expected != actual:
        raise ProcedureError(f"Los conteos o agregados restaurados no coinciden con el manifiesto de {database}")


def _entorno_for(entorno_name: str, compose_project: str) -> entornos.Entorno:
    entorno = entornos.ENTORNOS.get(entorno_name)
    if entorno is None:
        raise ProcedureError(f"Entorno desconocido {entorno_name!r}")
    if compose_project != entorno.proyecto:
        raise ProcedureError(
            f"Destino rechazado: el entorno {entorno.nombre} usa el proyecto {entorno.proyecto}, no {compose_project}"
        )
    return entorno


def create_backup(output_dir: Path, compose_project: str, writes_paused: bool,
                  entorno_name: str = "demo") -> dict[str, Any]:
    if not writes_paused:
        raise ProcedureError("Se requiere --confirm-writes-paused tras pausar escrituras y reconciliar reservas")
    entorno = _entorno_for(entorno_name, compose_project)
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise ProcedureError(f"El directorio destino ya existe; no se sobrescribirá: {output_dir}")
    old_umask = os.umask(0o077)
    try:
        output_dir.mkdir(mode=0o700, parents=True)
    finally:
        os.umask(old_umask)
    os.chmod(output_dir, 0o700)

    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "created_utc": utc_now(),
        "compose_project": compose_project,
        "entorno": entorno.nombre,
        "status": "running",
        "verified": False,
        "databases": {},
    }
    manifest_path = output_dir / "manifest.json"
    write_json_private(manifest_path, manifest)
    try:
        for name in DATABASES:
            container, service = resolve_primary_container(entorno, name)
            image = run_checked(["docker", "inspect", "--format", "{{.Config.Image}}", container]).stdout.decode().strip()
            manifest["databases"][name] = {
                "container": container,
                "service": service,
                "image": image,
                "before": _read_database_snapshot(name, container),
            }
            if name == "eventos_db":
                reservations = manifest["databases"][name]["before"]["stats"]["reservas_por_estado_evento"]
                if any(row["estado"] == "reservada" for row in reservations):
                    raise ProcedureError("Hay reservas pendientes; reconcílialas antes del respaldo")
            write_json_private(manifest_path, manifest)

        majors = {postgres_major(value["before"]) for value in manifest["databases"].values()}
        if majors != {15}:
            raise ProcedureError(f"Las tres bases deben usar PostgreSQL 15; versiones encontradas: {sorted(majors)}")

        for name in DATABASES:
            dump_path = output_dir / f"{name}.dump"
            partial_path = output_dir / f"{name}.dump.partial"
            command = ["docker", "exec", "-i", "-u", "postgres", manifest["databases"][name]["container"],
                       "pg_dump", "-U", "postgres", "-d", name, "--format=custom", "--no-owner", "--no-acl"]
            fd = os.open(partial_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as archive:
                run_checked(command, stdout=archive, timeout=3600)
                archive.flush()
                os.fsync(archive.fileno())
            os.replace(partial_path, dump_path)
            os.chmod(dump_path, 0o600)
            manifest["databases"][name]["dump_file"] = dump_path.name
            manifest["databases"][name]["sha256"] = file_sha256(dump_path)
            write_json_private(manifest_path, manifest)

        for name in DATABASES:
            manifest["databases"][name]["after"] = _read_database_snapshot(name, manifest["databases"][name]["container"])
            assert_same_stats(manifest["databases"][name]["before"]["stats"],
                              manifest["databases"][name]["after"]["stats"], name)
            compare_schema(manifest["databases"][name]["before"]["schema"],
                           manifest["databases"][name]["after"]["schema"], name)
            if manifest["databases"][name]["before"]["postgres"]["version_num"] != manifest["databases"][name]["after"]["postgres"]["version_num"]:
                raise ProcedureError(f"La versión PostgreSQL cambió durante el respaldo de {name}")

        sums = "".join(f"{manifest['databases'][name]['sha256']}  {name}.dump\n" for name in DATABASES)
        sums_path = output_dir / "SHA256SUMS"
        fd = os.open(sums_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(sums)
            stream.flush()
            os.fsync(stream.fileno())
        manifest["status"] = "complete"
        manifest["completed_utc"] = utc_now()
        write_json_private(manifest_path, manifest)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["failure"] = _redact(str(exc))
        manifest["failed_utc"] = utc_now()
        try:
            write_json_private(manifest_path, manifest)
        except OSError:
            pass
        raise


def _check_backup_files(backup_dir: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("format_version") != FORMAT_VERSION or manifest.get("status") != "complete":
        raise ProcedureError("El manifiesto no declara un respaldo completo de formato compatible")
    if not (backup_dir / "SHA256SUMS").is_file():
        raise ProcedureError("Falta SHA256SUMS")
    for name in DATABASES:
        item = manifest.get("databases", {}).get(name, {})
        filename = item.get("dump_file")
        path = backup_dir / f"{name}.dump"
        if filename != path.name or not path.is_file():
            raise ProcedureError(f"Falta el dump esperado de {name}")
        if file_sha256(path) != item.get("sha256"):
            raise ProcedureError(f"El hash SHA-256 no coincide para {name}")
    listed: dict[str, str] = {}
    for line in (backup_dir / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, filename = line.split(maxsplit=1)
        filename = filename.lstrip("* ")
        if filename not in {f"{name}.dump" for name in DATABASES} or filename in listed:
            raise ProcedureError("SHA256SUMS contiene un nombre de archivo inesperado o repetido")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or file_sha256(backup_dir / filename) != digest:
            raise ProcedureError(f"Falló la comprobación de SHA256SUMS para {filename}")
        listed[filename] = digest
    if set(listed) != {f"{name}.dump" for name in DATABASES}:
        raise ProcedureError("SHA256SUMS no enumera exactamente los tres dumps")


def _restore_stats(container: str, db: str) -> dict[str, Any]:
    try:
        return json.loads(_psql(container, db, STATS_SQL[db]))
    except json.JSONDecodeError as exc:
        raise ProcedureError(f"La restauración de {db} no devolvió estadísticas JSON válidas") from exc


def _wait_for_initialized_postgres(container: str, timeout_seconds: int) -> None:
    """Avoid mistaking initdb's short-lived bootstrap server for the final server."""
    deadline = time.monotonic() + timeout_seconds
    init_complete = False
    consecutive_probes = 0
    marker = b"PostgreSQL init process complete; ready for start up."
    while time.monotonic() < deadline:
        if not init_complete:
            logs = subprocess.run(["docker", "logs", container], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, check=False)
            init_complete = marker in (logs.stdout or b"")
        if init_complete:
            probe = subprocess.run([
                "docker", "exec", container, "psql", "-X", "-A", "-t",
                "-U", "postgres", "-d", "postgres", "-c", "SELECT 1",
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if probe.returncode == 0 and probe.stdout.strip() == b"1":
                consecutive_probes += 1
                if consecutive_probes >= 2:
                    return
            else:
                consecutive_probes = 0
        time.sleep(1)
    raise ProcedureError("PostgreSQL temporal no completó initdb y dos comprobaciones de conexión")


def _run_migration_rehearsal(network: str, backup_id: str, db_container: str) -> None:
    expected_revisions = {"usuarios": "20261102_02", "compras": "20261102_03"}
    for service in ("usuarios", "compras"):
        image_tag = f"nexustix-backup-verify:{backup_id}-{service}"
        dockerfile = ROOT / service / "Dockerfile"
        run_checked(["docker", "build", "--tag", image_tag, "--file", str(dockerfile), str(ROOT)], timeout=1800)
        database = f"{service}_db"
        url = f"postgresql://postgres@restore-db:5432/{database}"
        run_checked([
            "docker", "run", "--rm", "--network", network,
            "--env", f"DATABASE_URL={url}",
            "--entrypoint", "alembic", image_tag, "-c", "alembic.ini", "upgrade", "head",
        ], timeout=1800)
        revision = _psql(db_container, database, "SELECT version_num FROM alembic_version")
        expected_rev = expected_revisions[service]
        if revision != expected_rev:
            raise ProcedureError(f"Ensayo de migración de {database}: revisión inesperada {revision!r} (esperada {expected_rev!r})")
        # La etiqueta es única de este ensayo; las capas en caché se conservan.
        subprocess.run(["docker", "image", "rm", image_tag], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


def verify_backup(backup_dir: Path, rehearse_migrations: bool = False, timeout_seconds: int = 90) -> dict[str, Any]:
    backup_dir = backup_dir.expanduser().resolve()
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ProcedureError("No se encontró manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _check_backup_files(backup_dir, manifest)
    major = postgres_major(manifest["databases"]["usuarios_db"]["before"])
    image = f"postgres:{major}-alpine"
    suffix = uuid.uuid4().hex[:10]
    network = f"nex-backup-check-net-{suffix}"
    container = f"nex-backup-check-db-{suffix}"
    result: dict[str, Any] = {
        "started_utc": utc_now(),
        "status": "running",
        "network": network,
        "container": container,
        "image": image,
        "migrations_rehearsed": False,
        "databases": {},
    }
    verification_path = backup_dir / "verification.json"
    network_created = False
    container_created = False
    try:
        run_checked(["docker", "network", "create", "--internal", "--label", "org.nexustix.purpose=backup-restore-verification", network])
        network_created = True
        run_checked([
            "docker", "run", "--detach", "--name", container, "--network", network,
            "--network-alias", "restore-db", "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,noexec",
            "--env", "POSTGRES_HOST_AUTH_METHOD=trust", image,
        ], timeout=1800)
        container_created = True
        _wait_for_initialized_postgres(container, timeout_seconds)

        for name in DATABASES:
            run_checked(["docker", "exec", container, "createdb", "-U", "postgres", name])
            archive = (backup_dir / f"{name}.dump").open("rb")
            with archive:
                run_checked([
                        "docker", "exec", "-i", container, "pg_restore", "-U", "postgres", "--exit-on-error",
                        "--no-owner", "--no-acl", "--dbname", name,
                ], stdin=archive, timeout=3600)
            actual = _restore_stats(container, name)
            expected = manifest["databases"][name]["before"]["stats"]
            assert_same_stats(expected, actual, name)
            result["databases"][name] = {"restore": "ok", "counts_match": True, "stats": actual}
            expected_schema = manifest["databases"][name]["before"].get("schema")
            if expected_schema is not None:
                compare_schema(expected_schema, json.loads(_psql(container, name, SCHEMA_SQL)), name)
                result["databases"][name]["schema_match"] = True
            else:
                # Manifiestos de versiones anteriores no registraban el esquema.
                result["databases"][name]["schema_match"] = None

        if rehearse_migrations:
            _run_migration_rehearsal(network, suffix, container)
            result["migrations_rehearsed"] = True
            for name in DATABASES:
                actual = _restore_stats(container, name)
                expected = manifest["databases"][name]["before"]["stats"]
                assert_same_stats(expected, actual, name)
                result["databases"][name]["after_migration_stats_match"] = True

        result["status"] = "verified"
        result["completed_utc"] = utc_now()
        write_json_private(verification_path, result)
        # Only resources created above are stopped/removed, and only after success.
        run_checked(["docker", "stop", container], timeout=60)
        run_checked(["docker", "rm", container], timeout=60)
        container_created = False
        run_checked(["docker", "network", "rm", network], timeout=60)
        network_created = False
        result["temporary_resources"] = "removed"
        write_json_private(verification_path, result)
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["failure"] = _redact(str(exc))
        result["failed_utc"] = utc_now()
        result["temporary_resources"] = "retained_for_diagnosis"
        write_json_private(verification_path, result)
        retained = []
        if container_created:
            retained.append(container)
        if network_created:
            retained.append(network)
        raise ProcedureError(f"{exc}. Se conservaron para diagnóstico: {', '.join(retained) or 'ninguno'}") from exc


def restore_backup(backup_dir: Path, compose_project: str, entorno_name: str, confirm_empty: bool) -> dict[str, Any]:
    """Disaster recovery: load a verified backup into EMPTY databases of one environment.

    Every target database must contain no tables; if any has tables nothing is
    restored. Only the database containers may be running (start them with
    ``docker compose up -d usuarios_db eventos_db compras_db``): the APIs would
    otherwise create their schema in the empty databases first.
    """
    if not confirm_empty:
        raise ProcedureError("Se requiere --confirm-empty-target")
    entorno = _entorno_for(entorno_name, compose_project)
    backup_dir = backup_dir.expanduser().resolve()
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    _check_backup_files(backup_dir, manifest)
    verification = backup_dir / "verification.json"
    if not verification.is_file() or json.loads(verification.read_text(encoding="utf-8")).get("status") != "verified":
        raise ProcedureError("El respaldo no tiene una verificación aislada exitosa (ejecute 'verify' primero)")
    targets = {}
    for name in DATABASES:
        container, service = resolve_primary_container(entorno, name)
        tables = _psql(container, name, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        if tables != "0":
            raise ProcedureError(f"{name} en {service} no está vacía ({tables} tablas); no se restaura nada")
        targets[name] = container
    result: dict[str, Any] = {"started_utc": utc_now(), "entorno": entorno.nombre, "databases": {}}
    for name, container in targets.items():
        with (backup_dir / f"{name}.dump").open("rb") as archive:
            run_checked(["docker", "exec", "-i", "-u", "postgres", container, "pg_restore", "-U", "postgres",
                         "--exit-on-error", "--no-owner", "--no-acl", "--dbname", name], stdin=archive, timeout=3600)
        assert_same_stats(manifest["databases"][name]["before"]["stats"], _restore_stats(container, name), name)
        expected_schema = manifest["databases"][name]["before"].get("schema")
        if expected_schema is not None:
            compare_schema(expected_schema, json.loads(_psql(container, name, SCHEMA_SQL)), name)
        result["databases"][name] = {"restore": "ok", "counts_match": True, "schema_match": expected_schema is not None}
    result["status"] = "restored"
    result["completed_utc"] = utc_now()
    write_json_private(backup_dir / f"restore-{entorno.nombre}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = root.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="respaldar las tres bases del entorno indicado")
    backup.add_argument("--entorno", choices=sorted(entornos.ENTORNOS), default="demo")
    backup.add_argument("--output-dir", required=True, type=Path)
    backup.add_argument("--compose-project", required=True, help="etiqueta Compose esperada; por seguridad no se autodetecta")
    backup.add_argument("--confirm-writes-paused", action="store_true",
                        help="confirma pausa de escrituras y reconciliación previa de reservas")
    verify = subparsers.add_parser("verify", help="restaurar y comparar en PostgreSQL temporal aislado")
    verify.add_argument("--backup-dir", required=True, type=Path)
    verify.add_argument("--rehearse-migrations", action="store_true",
                        help="construye Usuarios/Compras y migra únicamente las copias temporales")
    restore = subparsers.add_parser("restore", help="cargar un respaldo verificado en bases VACÍAS de un entorno")
    restore.add_argument("--entorno", choices=sorted(entornos.ENTORNOS), required=True)
    restore.add_argument("--compose-project", required=True)
    restore.add_argument("--backup-dir", required=True, type=Path)
    restore.add_argument("--confirm-empty-target", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.output_dir, args.compose_project, args.confirm_writes_paused, args.entorno)
            print(json.dumps({
                "status": result["status"], "backup_dir": str(args.output_dir.resolve()),
                "databases": {name: item["before"]["stats"] for name, item in result["databases"].items()},
                "verification_required": True,
            }, indent=2, ensure_ascii=False))
        elif args.command == "restore":
            result = restore_backup(args.backup_dir, args.compose_project, args.entorno, args.confirm_empty_target)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            result = verify_backup(args.backup_dir, args.rehearse_migrations)
            print(json.dumps({
                "status": result["status"], "migrations_rehearsed": result["migrations_rehearsed"],
                "databases": {name: {"restore": item["restore"], "counts_match": item["counts_match"],
                                     "schema_match": item.get("schema_match")}
                              for name, item in result["databases"].items()},
            }, indent=2, ensure_ascii=False))
        return 0
    except (ProcedureError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {_redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

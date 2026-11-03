"""Operaciones PostgreSQL sobre contenedores de un entorno NEXUSTIX (vía docker exec).

El SQL se envía por stdin para que valores sensibles (p. ej. la contraseña del
rol de replicación) no aparezcan en la lista de procesos ni en los argumentos.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import entornos


class PgError(RuntimeError):
    pass


class DosPrimariosError(PgError):
    """Dos nodos aceptan escrituras a la vez: nunca se actúa automáticamente."""


def psql(container: str, database: str, sql: str, *, timeout: int = 60) -> str:
    sql = sql.strip()
    if not sql.endswith(";"):
        sql += ";"
    result = subprocess.run(
        ["docker", "exec", "-i", "-u", "postgres", container,
         "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database, "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()[-1:] or ["sin detalle"]
        raise PgError(f"psql falló en {container[:12]}/{database}: {detail[0][:300]}")
    return result.stdout.strip()


def psql_json(container: str, database: str, sql: str) -> dict | list:
    raw = psql(container, database, sql)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PgError(f"La consulta en {database} no devolvió JSON válido") from exc


def is_running(container: str) -> bool:
    try:
        return entornos.inspect(container, "{{.State.Running}}") == "true"
    except RuntimeError:
        return False


@dataclass(frozen=True)
class Nodo:
    servicio: str
    container: str | None
    running: bool
    en_recuperacion: bool | None  # True = standby, False = primario, None = no consultable


def nodos(entorno: entornos.Entorno, base: str) -> list[Nodo]:
    """Estado de los nodos <base> y <base>_replica (si existe) del entorno."""
    result = []
    for servicio in (base, f"{base}_replica"):
        ids = entornos.containers(entorno, servicio)
        if not ids:
            continue
        if len(ids) > 1:
            raise PgError(f"Hay {len(ids)} contenedores para {servicio}; se esperaba uno")
        container = ids[0]
        running = is_running(container)
        in_recovery = None
        if running:
            try:
                in_recovery = psql(container, base, "SELECT pg_is_in_recovery();") == "t"
            except PgError:
                in_recovery = None
        result.append(Nodo(servicio, container, running, in_recovery))
    return result


def primario(entorno: entornos.Entorno, base: str) -> Nodo:
    """Único nodo que acepta escrituras; error si no hay ninguno o hay dos."""
    candidatos = [n for n in nodos(entorno, base) if n.running and n.en_recuperacion is False]
    if len(candidatos) > 1:
        raise DosPrimariosError(
            f"{base}: {', '.join(n.servicio for n in candidatos)} aceptan escrituras a la vez. "
            "Detenga uno manualmente y revise los datos antes de continuar."
        )
    if not candidatos:
        raise PgError(f"{base}: no hay un nodo primario en ejecución en el proyecto {entorno.proyecto}")
    return candidatos[0]

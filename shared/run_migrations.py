"""Guarded ``alembic upgrade head`` executed before a service starts.

Run from the service directory (where alembic.ini lives):

    python -m shared.run_migrations

Policy (fail closed, never destructive):
- Empty database                         -> create the schema (fresh install).
- alembic_version at a known revision    -> upgrade forward to head (update).
- alembic_version already at head        -> nothing to do.
- Unknown revision (newer/foreign code)  -> refuse; do not start the service.
- Tables but no alembic_version (legacy) -> refuse unless the operator sets
  NEXUSTIX_ALLOW_LEGACY_BASELINE=1 after taking and verifying a backup.

A PostgreSQL advisory lock serializes concurrent runs, e.g. compras1 and
compras2 starting at the same time against the shared compras_db.
Set NEXUSTIX_AUTO_MIGRATE=0 to skip and keep migrations fully manual.
"""

from __future__ import annotations

import os
import sys
import time

MIGRATION_LOCK_KEY = 7419251031


def decide_action(tables: set[str], current: str | None, known: set[str], head: str,
                  allow_legacy: bool) -> tuple[str, str]:
    """Return (action, message); action is 'upgrade', 'noop' or 'refuse'."""
    app_tables = tables - {"alembic_version"}
    if current is None:
        if not app_tables:
            return "upgrade", "Base vacía: se crea el esquema desde cero."
        if allow_legacy:
            return "upgrade", "Esquema legacy sin alembic_version: baseline autorizado explícitamente."
        return "refuse", (
            "La base contiene tablas pero no alembic_version (instalación legacy). "
            "Haz y verifica un respaldo y define NEXUSTIX_ALLOW_LEGACY_BASELINE=1 para aplicar el baseline."
        )
    if current not in known:
        return "refuse", f"Revisión desconocida {current!r}; el código no corresponde a esta base."
    if current == head:
        return "noop", f"Esquema ya en la revisión {head}."
    return "upgrade", f"Actualización de esquema {current} -> {head}."


def _wait_for_database(engine, timeout_seconds: int) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def main() -> int:
    if os.getenv("NEXUSTIX_AUTO_MIGRATE", "1") != "1":
        print("[migraciones] NEXUSTIX_AUTO_MIGRATE=0: se omiten (modo manual).")
        return 0

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.pool import NullPool

    from shared.db_engine import read_database_url

    try:
        url = read_database_url()
    except RuntimeError:
        print("[migraciones] ERROR: configure DATABASE_URL o DATABASE_URL_FILE válido", file=sys.stderr)
        return 2
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    known = {revision.revision for revision in script.walk_revisions()}
    engine = create_engine(url, poolclass=NullPool,
                           connect_args={"connect_timeout": 5} if url.startswith("postgresql") else {})
    try:
        _wait_for_database(engine, int(os.getenv("NEXUSTIX_MIGRATION_WAIT_SECONDS", "90")))
    except OperationalError:
        print("[migraciones] ERROR: PostgreSQL no respondió a tiempo", file=sys.stderr)
        return 3

    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
            connection.commit()
        try:
            tables = set(inspect(connection).get_table_names())
            current = None
            if "alembic_version" in tables:
                current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            action, message = decide_action(
                tables, current, known, head,
                os.getenv("NEXUSTIX_ALLOW_LEGACY_BASELINE") == "1",
            )
            print(f"[migraciones] {message}")
            if action == "refuse":
                connection.rollback()
                return 4
            if action == "upgrade":
                # env.py reuses this connection, so the advisory lock covers
                # the whole upgrade and the DDL commits atomically here.
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.commit()
                print(f"[migraciones] Esquema en la revisión {head}.")
            else:
                connection.rollback()
        finally:
            if postgres:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
                connection.commit()
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

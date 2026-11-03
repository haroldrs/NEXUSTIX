"""Respaldo lógico real: la huella de esquema y los agregados sobreviven a pg_dump/pg_restore.

Opt-in: NEXUSTIX_TEST_PG_BIN (pg_dump, pg_restore) y NEXUSTIX_TEST_PG_ADMIN_URL.
Usa el mismo SQL y las mismas comparaciones que scripts/backup_main_databases.py,
con las mismas opciones de pg_dump/pg_restore; sin Docker. Así se evita que
'verify' rechace respaldos correctos (falsos negativos) o acepte restauraciones
incompletas.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from pg_helpers import ROOT, load_service, migrate

BIN = os.getenv("NEXUSTIX_TEST_PG_BIN")
pytestmark = pytest.mark.skipif(not (BIN and os.getenv("NEXUSTIX_TEST_PG_ADMIN_URL")),
                                reason="Opt-in: NEXUSTIX_TEST_PG_BIN y NEXUSTIX_TEST_PG_ADMIN_URL")
sys.path.insert(0, str(ROOT / "scripts"))


def tool(name):
    return str(Path(BIN) / (name + (".exe" if os.name == "nt" else "")))


def pg_env(url):
    parsed = make_url(url)
    return {**os.environ, "PGHOST": parsed.host, "PGPORT": str(parsed.port), "PGUSER": parsed.username,
            "PGPASSWORD": parsed.password or ""}, parsed.database


def query_json(url, sql):
    from sqlalchemy import create_engine
    engine = create_engine(url)
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute(sql)
        return json.loads(cursor.fetchone()[0])
    finally:
        raw.close()
        engine.dispose()


def test_dump_restore_keeps_schema_fingerprint_and_aggregates(pg_url_factory, keys, monkeypatch, tmp_path):
    import backup_main_databases as backup
    from test_pg_integracion import EventosEnProceso, buy, buyer

    sources = {"usuarios_db": pg_url_factory(), "eventos_db": pg_url_factory(), "compras_db": pg_url_factory()}
    assert migrate("usuarios", sources["usuarios_db"], monkeypatch) == 0
    assert migrate("compras", sources["compras_db"], monkeypatch) == 0
    ev_main, ev_models, ev_db = load_service("eventos", sources["eventos_db"], monkeypatch)
    ev_main.esperar_bd()
    with ev_db.SessionLocal() as db:
        db.add(ev_models.Evento(id=900003, nombre="Sintético respaldo", stock=5, stock_inicial=5))
        db.commit()
    co_main, _, co_db = load_service("compras", sources["compras_db"], monkeypatch)
    adapter = EventosEnProceso(ev_main, ev_db.SessionLocal)
    monkeypatch.setattr(co_main.requests, "post", adapter.post)
    monkeypatch.setattr(co_main.requests, "get", adapter.get)
    for i in range(2):  # datos reales en las tres tablas de Compras y en reservas_stock
        assert buy({"co": co_main, "co_db": co_db}, f"bk-{i}-{uuid.uuid4().hex}", buyer(co_main, 800 + i), event_id=900003)[0] == 200
    co_db.engine.dispose()
    ev_db.engine.dispose()

    for name, url in sources.items():
        before_stats = query_json(url, backup.STATS_SQL[name])
        before_schema = query_json(url, backup.SCHEMA_SQL)
        env, database = pg_env(url)
        archive = tmp_path / f"{name}.dump"
        subprocess.run([tool("pg_dump"), "-d", database, "--format=custom", "--no-owner", "--no-acl", "-f", str(archive)],
                       check=True, env=env, capture_output=True, stdin=subprocess.DEVNULL)
        target = pg_url_factory()
        target_env, target_db = pg_env(target)
        subprocess.run([tool("pg_restore"), "--exit-on-error", "--no-owner", "--no-acl", "--dbname", target_db, str(archive)],
                       check=True, env=target_env, capture_output=True, stdin=subprocess.DEVNULL)
        backup.assert_same_stats(before_stats, query_json(target, backup.STATS_SQL[name]), name)
        restored_schema = query_json(target, backup.SCHEMA_SQL)
        backup.compare_schema(before_schema, restored_schema, name)
        assert restored_schema["tables"] and restored_schema["constraints"], name
        if name == "compras_db":
            assert restored_schema["alembic_revision"] == "20261102_03"
            assert restored_schema["row_counts"]["compras"] == 2
            assert restored_schema["row_counts"]["purchase_idempotency"] == 2
        if name == "usuarios_db":
            assert restored_schema["alembic_revision"] == "20261102_02"

    # Una restauración incompleta sí se detecta.
    damaged = dict(before_schema, constraints=before_schema["constraints"][1:])
    with pytest.raises(backup.ProcedureError):
        backup.compare_schema(before_schema, damaged, "eventos_db")

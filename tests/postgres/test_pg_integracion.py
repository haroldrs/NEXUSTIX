"""Integración sobre PostgreSQL real (desechable): migraciones, saga Compras↔Eventos y bloqueos.

Las réplicas de Compras comparten estado solo a través de PostgreSQL, así que
varios hilos con sesiones independientes reproducen fielmente dos réplicas
compitiendo por la misma fila. Eventos se ejecuta en proceso sobre su propia
base PostgreSQL; un adaptador traduce las llamadas HTTP de Compras.
"""
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

from pg_helpers import ROOT, load_service, migrate

pytestmark = pytest.mark.skipif(not os.getenv("NEXUSTIX_TEST_PG_ADMIN_URL"),
                                reason="Opt-in: requiere NEXUSTIX_TEST_PG_ADMIN_URL (PostgreSQL desechable)")
SYNTHETIC_EVENT = 900001


class Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class EventosEnProceso:
    """Sustituye requests.get/post de Compras por llamadas a las funciones de Eventos."""

    def __init__(self, eventos_main, sessions):
        self.ev = eventos_main
        self.sessions = sessions
        self.down = False
        self.calls = []
        self._lock = threading.Lock()

    def _call(self, fn, *args):
        db = self.sessions()
        try:
            return Resp(200, fn(*args, db, None))
        except HTTPException as exc:
            db.rollback()
            return Resp(exc.status_code, {"detail": exc.detail})
        finally:
            db.close()

    def post(self, url, params=None, headers=None, timeout=None):
        import requests
        if self.down:
            raise requests.exceptions.ConnectionError("Eventos detenido (simulado)")
        _, _, event_id, action = urlparse(url).path.split("/")
        p = params or {}
        with self._lock:
            self.calls.append(action)
        if action == "reservar":
            return self._call(self.ev.reservar_entradas, int(event_id), int(p["cantidad"]), int(p.get("reloj_t", 0)), p["operacion_id"])
        if action == "confirmar":
            return self._call(self.ev.confirmar_reserva, int(event_id), p["operacion_id"], int(p.get("reloj_t", 0)))
        if action == "liberar":
            return self._call(self.ev.liberar_entradas, int(event_id), int(p["cantidad"]), int(p.get("reloj_t", 0)), p["operacion_id"])
        raise AssertionError(url)

    def get(self, url, headers=None, timeout=None):
        import requests
        if self.down:
            raise requests.exceptions.ConnectionError("Eventos detenido (simulado)")
        path = urlparse(url).path
        if path == "/eventos/reservas/pendientes":
            return self._call(self.ev.reservas_pendientes)
        if path == "/eventos/auditoria/stock":
            return self._call(self.ev.auditoria_eventos)
        if path.startswith("/eventos/reservas/"):
            return self._call(self.ev.consultar_reserva_por_operacion, path.rsplit("/", 1)[1])
        raise AssertionError(url)


@pytest.fixture
def saga(pg_url_factory, keys, monkeypatch):
    eventos_url, compras_url = pg_url_factory(), pg_url_factory()
    ev_main, ev_models, ev_db = load_service("eventos", eventos_url, monkeypatch)
    ev_main.esperar_bd()  # DDL idempotente real de Eventos sobre PostgreSQL
    with ev_db.SessionLocal() as db:
        db.add(ev_models.Evento(id=SYNTHETIC_EVENT, nombre="Sintético PG", stock=1, stock_inicial=1))
        db.commit()
    assert migrate("compras", compras_url, monkeypatch) == 0
    co_main, co_models, co_db = load_service("compras", compras_url, monkeypatch)
    adapter = EventosEnProceso(ev_main, ev_db.SessionLocal)
    monkeypatch.setattr(co_main.requests, "post", adapter.post)
    monkeypatch.setattr(co_main.requests, "get", adapter.get)
    yield {"ev": ev_main, "ev_models": ev_models, "ev_db": ev_db, "co": co_main,
           "co_models": co_models, "co_db": co_db, "adapter": adapter}
    co_db.engine.dispose()
    ev_db.engine.dispose()


def buyer(co, user_id):
    return co.Principal(user_id=user_id, role="comprador", subject=f"nexustix-usuarios|{user_id}")


def buy(s, key, principal, event_id=SYNTHETIC_EVENT, quantity=1):
    """(status, cuerpo) como los vería el cliente HTTP (FastAPI convierte HTTPException en respuesta)."""
    co = s["co"]
    with s["co_db"].SessionLocal() as db:
        try:
            result = co.crear_compra(co.CompraRequest(evento_id=event_id, cantidad=quantity), key, db, principal, None, None)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}
    if isinstance(result, dict):
        return 200, result
    import json
    return result.status_code, json.loads(result.body)


def stock(s, event_id=SYNTHETIC_EVENT):
    with s["ev_db"].SessionLocal() as db:
        return db.get(s["ev_models"].Evento, event_id).stock


def set_stock(s, value, event_id=SYNTHETIC_EVENT):
    with s["ev_db"].SessionLocal() as db:
        evento = db.get(s["ev_models"].Evento, event_id)
        evento.stock = evento.stock_inicial = value
        db.commit()


def test_fresh_install_migrations_reach_expected_revisions_and_are_idempotent(pg_url_factory, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    for service, revision, tables in (("usuarios", "20261102_02", {"usuarios", "refresh_sessions"}),
                                      ("compras", "20261102_03", {"compras", "journal_compras", "purchase_idempotency"})):
        url = pg_url_factory()
        assert migrate(service, url, monkeypatch) == 0
        assert migrate(service, url, monkeypatch) == 0, "segunda ejecución = no-op"
        engine = create_engine(url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == revision
            assert tables <= set(inspect(connection).get_table_names())
        engine.dispose()


def test_two_replicas_migrating_at_once_are_serialized(pg_url_factory, keys):
    from sqlalchemy import create_engine, text

    url = pg_url_factory()
    env = {**os.environ, "DATABASE_URL": url, "NEXUSTIX_AUTO_MIGRATE": "1", "PYTHONPATH": str(ROOT)}
    env.pop("DATABASE_URL_FILE", None)
    procs = [subprocess.Popen([sys.executable, "-m", "shared.run_migrations"], cwd=ROOT / "compras", env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(2)]
    outputs = [p.communicate(timeout=120)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outputs
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
    engine.dispose()


def test_last_ticket_under_concurrency_is_sold_once(saga):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: buy(saga, f"last-{i}-{uuid.uuid4().hex}", buyer(saga["co"], 100 + i)), range(6)))
    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409, 409, 409, 409, 409], results
    assert stock(saga) == 0
    with saga["co_db"].SessionLocal() as db:
        assert db.query(saga["co_models"].Compra).filter_by(evento_id=SYNTHETIC_EVENT).count() == 1
    with saga["ev_db"].SessionLocal() as db:
        rows = db.query(saga["ev_models"].ReservaStock).filter_by(evento_id=SYNTHETIC_EVENT).all()
        assert [r.estado for r in rows] == ["confirmada"]


def test_same_key_from_both_replicas_creates_one_purchase(saga):
    set_stock(saga, 5)
    key = f"same-{uuid.uuid4().hex}"
    principal = buyer(saga["co"], 200)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: buy(saga, key, principal), range(6)))
    assert {status for status, _ in results} == {200}, results
    assert len({body["id"] for _, body in results}) == 1
    assert stock(saga) == 4, "el stock baja una sola vez"
    with saga["co_db"].SessionLocal() as db:
        assert db.query(saga["co_models"].Compra).count() == 1
        assert db.query(saga["co_models"].IdempotencyRequest).one().state == "completed"
    assert saga["adapter"].calls.count("reservar") == 1
    status, _ = buy(saga, key, principal, quantity=2)
    assert status == 409, "la misma clave con otro contenido se rechaza"


def test_events_outage_is_ambiguous_then_retry_completes_once(saga):
    set_stock(saga, 3)
    key = f"outage-{uuid.uuid4().hex}"
    principal = buyer(saga["co"], 300)
    saga["adapter"].down = True
    status, body = buy(saga, key, principal)
    assert status == 503 and body["detail"]["recuperacion_requerida"] is True
    with saga["co_db"].SessionLocal() as db:
        claim = db.query(saga["co_models"].IdempotencyRequest).one()
        assert claim.state == "processing" and db.query(saga["co_models"].Compra).count() == 0
    assert stock(saga) == 3
    saga["adapter"].down = False
    status, body = buy(saga, key, principal)
    assert status == 200 and body["operacion_id"] == claim.operacion_id
    assert stock(saga) == 2
    with saga["co_db"].SessionLocal() as db:
        assert db.query(saga["co_models"].Compra).count() == 1


def test_claim_held_by_other_replica_times_out_as_retryable_503(saga, monkeypatch):
    from sqlalchemy import text

    co = saga["co"]
    set_stock(saga, 3)
    key = f"held-{uuid.uuid4().hex}"
    principal = buyer(co, 400)
    status, first = buy(saga, key, principal)
    assert status == 200
    monkeypatch.setattr(co, "CLAIM_LOCK_TIMEOUT_MS", 1000)
    holder = saga["co_db"].engine.connect()
    try:
        holder.execute(text("SELECT 1 FROM purchase_idempotency WHERE idempotency_key = :k FOR UPDATE"), {"k": key})
        status, busy = buy(saga, key, principal)
        assert status == 503 and busy["detail"]["en_proceso"] is True
    finally:
        holder.rollback()
        holder.close()
    status, replay = buy(saga, key, principal)
    assert status == 200 and replay["id"] == first["id"]


def test_recovery_skips_operation_locked_by_live_request_and_sweep_is_exclusive(saga):
    from sqlalchemy import text

    co, models = saga["co"], saga["co_models"]
    from datetime import datetime, timedelta, timezone
    with saga["co_db"].SessionLocal() as db:
        db.add(models.IdempotencyRequest(idempotency_key="live-key", auth_subject="nexustix-usuarios|1",
                                         request_hash="a" * 64, operacion_id="live-op", state="processing",
                                         created_at=datetime.now(timezone.utc) - timedelta(seconds=600)))
        db.commit()
    holder = saga["co_db"].engine.connect()
    try:
        holder.execute(text("SELECT 1 FROM purchase_idempotency WHERE operacion_id = 'live-op' FOR UPDATE"))
        with saga["co_db"].SessionLocal() as db:
            result = co.ejecutar_recuperacion(db, permitir_compensacion=True, incluir_auditoria=False, origen="test")
        assert result["contadores"]["omitidas_en_proceso"] == 1
    finally:
        holder.rollback()
        holder.close()
    lock = saga["co_db"].engine.connect()
    try:
        assert lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": co.RECOVERY_LOCK_KEY}).scalar()
        assert co.ejecutar_barrido_automatico()["motivo"] == "otra_replica_ejecuta_la_recuperacion"
    finally:
        lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": co.RECOVERY_LOCK_KEY})
        lock.close()
    assert co.ejecutar_barrido_automatico()["status"] in {"recuperacion_completa", "recuperacion_incompleta"}


def test_consistency_audit_is_true_after_the_saga(saga):
    set_stock(saga, 4)
    for i in range(3):
        assert buy(saga, f"audit-{i}-{uuid.uuid4().hex}", buyer(saga["co"], 500 + i))[0] == 200
    with saga["co_db"].SessionLocal() as db:
        audit = saga["co"].auditar_consistencia(db, None)
    synthetic = next(e for e in audit["reporte_por_evento"] if e["evento_id"] == SYNTHETIC_EVENT)
    assert synthetic["es_consistente"] and synthetic["stock_actual_disponible"] == 1
    assert audit["reservas_pendientes"] == [] and audit["compras_sin_reserva_confirmada"] == []


def test_operational_sql_runs_on_real_postgres(pg_url_factory, monkeypatch):
    """El SQL de respaldo, reset y replicación se ejecuta tal cual sobre PostgreSQL 15."""
    import json
    from sqlalchemy import create_engine

    sys.path.insert(0, str(ROOT / "scripts"))
    import backup_main_databases as backup

    urls = {}
    for service in ("usuarios", "compras"):
        urls[f"{service}_db"] = pg_url_factory()
        assert migrate(service, urls[f"{service}_db"], monkeypatch) == 0
    urls["eventos_db"] = pg_url_factory()
    ev_main, _, ev_db = load_service("eventos", urls["eventos_db"], monkeypatch)
    ev_main.esperar_bd()
    ev_db.engine.dispose()
    for name, url in urls.items():
        engine = create_engine(url)
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute(backup.STATS_SQL[name])
            json.loads(cursor.fetchone()[0])
            cursor.execute(backup.SCHEMA_SQL)
            schema = json.loads(cursor.fetchone()[0])
            assert schema["tables"] and isinstance(schema["row_counts"], dict)
            if name == "compras_db":
                assert schema["alembic_revision"] == "20261102_03"
                assert any(c.endswith("ck_purchase_idempotency_state:c") for c in schema["constraints"])
                cursor.execute("TRUNCATE TABLE notificaciones, outbox, entradas, pagos_simulados, ordenes, compras, purchase_idempotency, journal_compras RESTART IDENTITY;")
            if name == "eventos_db":
                assert any("ck_eventos_stock_inicial_no_negativo" in c for c in schema["constraints"])
                assert "eventos.stock_inicial:integer:YES" in schema["columns"]
            raw.rollback()
        finally:
            raw.close()
            engine.dispose()

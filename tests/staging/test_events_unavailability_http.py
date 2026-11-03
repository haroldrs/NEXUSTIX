"""Events outage validation against the isolated staging Compose project only."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "nexustix-auth-staging"
COMPOSE = ROOT / "docker-compose.staging.yml"
FRONTEND = "http://localhost:18080"
USERS = "http://localhost:18081"
EVENTS = "http://localhost:18082"
PURCHASE1 = "http://localhost:18083"
PURCHASE2 = "http://localhost:18084"
LB = "http://localhost:18085"
PORTS = {"frontend": 18080, "usuarios": 18081, "eventos": 18082,
         "compras1": 18083, "compras2": 18084, "compras_lb": 18085}
pytestmark = pytest.mark.skipif(
    os.getenv("NEXUSTIX_STAGING_HTTP") != "1",
    reason="Requiere habilitación explícita del staging aislado",
)


def _env() -> dict[str, str]:
    value = os.getenv("NEXUSTIX_STAGING_SECRETS_DIR")
    assert value and Path(value).expanduser().is_dir(), "faltan secretos de staging"
    result = os.environ.copy()
    result["NEXUSTIX_STAGING_SECRETS_DIR"] = str(Path(value).expanduser().resolve())
    return result


def _compose(*args: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE), *args],
        cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=timeout,
    )


def _psql(service: str, database: str, sql: str) -> str:
    assert service in {"usuarios_db", "eventos_db", "compras_db"}
    result = _compose("exec", "-T", service, "psql", "-U", "postgres", "-d", database,
                      "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql, timeout=20)
    assert result.returncode == 0, f"consulta staging falló en {service} (código {result.returncode})"
    return result.stdout.strip()


def _container_rows() -> dict[str, dict]:
    result = _compose("ps", "--all", "--format", "json")
    assert result.returncode == 0, "no se pudo consultar el proyecto Compose de staging"
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    expected = set(PORTS) | {"usuarios_db", "eventos_db", "compras_db"}
    assert len(rows) == 9 and {r.get("Service") for r in rows} == expected
    assert all(r.get("Project") == PROJECT for r in rows)
    return {r["Service"]: r for r in rows}


def _preflight() -> dict[str, str]:
    config = _compose("config", "--quiet")
    assert config.returncode == 0, "Compose staging config --quiet falló (salida omitida)"
    rows = _container_rows()
    for name, row in rows.items():
        assert row.get("State") == "running", f"servicio staging no activo: {name}"
        if name in PORTS:
            ports = {p.get("PublishedPort") for p in row.get("Publishers", [])
                     if p.get("PublishedPort")}
            assert ports == {PORTS[name]}, f"puerto inesperado en {name}"
        if name.endswith("_db"):
            assert row.get("Health") == "healthy", f"base staging no saludable: {name}"
    assert _psql("usuarios_db", "usuarios_db", "SELECT version_num FROM alembic_version") == "20261022_01"
    assert _psql("compras_db", "compras_db", "SELECT version_num FROM alembic_version") == "20261023_02"
    for url in (FRONTEND, USERS, EVENTS, PURCHASE1, PURCHASE2, LB):
        response = requests.get(url if url == FRONTEND else f"{url}/health", timeout=8)
        assert response.status_code == 200, f"health HTTP {response.status_code} en {url}"
        if url != FRONTEND:
            assert response.json().get("db") == "connected", f"DB desconectada en {url}"
    return {"compose_project": PROJECT, "service_count": str(len(rows)),
            "ports": "18080-18085", "database_health": "3 healthy",
            "users_revision": "20261022_01", "purchases_revision": "20261023_02"}


def _register_buyer(session: requests.Session, suffix: str) -> dict:
    email = f"qa-events-down-{suffix}@example.com"
    password = secrets.token_urlsafe(28)
    response = session.post(f"{USERS}/auth/register", json={
        "nombre": "Comprador QA disponibilidad Eventos", "email": email, "password": password,
    }, timeout=10)
    del password
    assert response.status_code == 201, f"registro sintético HTTP {response.status_code}"
    result = response.json()
    assert result.get("user", {}).get("role") == "comprador"
    return {"id": int(result["user"]["id"]), "email": email,
            "token": result["access_token"],
            "subject": f"nexustix-usuarios|{result['user']['id']}"}


def _snapshot(event_id: int, event_name: str, key: str) -> dict:
    qevent = "'" + event_name.replace("'", "''") + "'"
    qkey = "'" + key.replace("'", "''") + "'"
    raw_event = _psql("eventos_db", "eventos_db", f"""
SELECT json_build_object(
 'event', (SELECT json_build_object('id',id,'name',nombre,'stock',stock) FROM eventos WHERE id={event_id}),
 'reservations', coalesce((SELECT json_agg(json_build_object('operation_id',operacion_id,'quantity',cantidad,'state',estado) ORDER BY operacion_id) FROM reservas_stock WHERE evento_id={event_id}),'[]'::json)
)::text
""")
    raw_purchase = _psql("compras_db", "compras_db", f"""
SELECT json_build_object(
 'purchases', coalesce((SELECT json_agg(json_build_object('id',id,'user_id',usuario_id,'subject',auth_subject,'event_id',evento_id,'quantity',cantidad,'state',estado,'operation_id',operacion_id) ORDER BY id) FROM compras WHERE evento_id={event_id}),'[]'::json),
 'claim', (SELECT json_build_object('key',idempotency_key,'subject',auth_subject,'request_hash',request_hash,'operation_id',operacion_id,'state',state,'status',response_status,'body',response_body) FROM purchase_idempotency WHERE idempotency_key={qkey}),
 'commits', (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id' IN (SELECT id::text FROM compras WHERE evento_id={event_id}))
)::text
""")
    return {"eventos": json.loads(raw_event), "compras": json.loads(raw_purchase)}


def _cleanup(event_id: int, event_name: str, key: str, buyer: dict,
             operation_id: str, purchase_id: int) -> dict:
    qname = "'" + event_name.replace("'", "''") + "'"
    qemail = "'" + buyer["email"].replace("'", "''") + "'"
    qsubject = "'" + buyer["subject"].replace("'", "''") + "'"
    qkey = "'" + key.replace("'", "''") + "'"
    qop = "'" + operation_id.replace("'", "''") + "'"
    exact = _snapshot(event_id, event_name, key)
    assert exact["eventos"] == {
        "event": {"id": event_id, "name": event_name, "stock": 1},
        "reservations": [{"operation_id": operation_id, "quantity": 1, "state": "confirmada"}],
    }, "estado de Eventos inesperado: se conserva el fixture"
    assert len(exact["compras"]["purchases"]) == 1
    purchase = exact["compras"]["purchases"][0]
    assert purchase == {"id": purchase_id, "user_id": buyer["id"], "subject": buyer["subject"],
                        "event_id": event_id, "quantity": 1, "state": "confirmada",
                        "operation_id": operation_id}, "compra inesperada: se conserva el fixture"
    claim = exact["compras"]["claim"]
    assert claim and claim["key"] == key and claim["subject"] == buyer["subject"]
    assert claim["operation_id"] == operation_id and claim["state"] == "completed"
    assert claim["status"] == 200 and claim["body"].get("confirmacion_reserva") == "confirmada"
    assert exact["compras"]["commits"] == 1
    assert _psql("eventos_db", "eventos_db", f"SELECT count(*) FROM reservas_stock WHERE evento_id={event_id} AND operacion_id<>{qop}") == "0"
    assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM compras WHERE (auth_subject={qsubject} OR usuario_id={buyer['id']}) AND id<>{purchase_id}") == "0"
    assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM purchase_idempotency WHERE auth_subject={qsubject} AND idempotency_key<>{qkey}") == "0"

    _psql("compras_db", "compras_db", f"""
BEGIN;
DO $$ BEGIN
 IF (SELECT count(*) FROM compras WHERE id={purchase_id} AND evento_id={event_id} AND operacion_id={qop} AND auth_subject={qsubject} AND estado='confirmada') <> 1 THEN RAISE EXCEPTION 'synthetic purchase guard'; END IF;
 IF (SELECT count(*) FROM purchase_idempotency WHERE idempotency_key={qkey} AND operacion_id={qop} AND auth_subject={qsubject} AND state='completed') <> 1 THEN RAISE EXCEPTION 'synthetic claim guard'; END IF;
 IF (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id'='{purchase_id}') <> 1 THEN RAISE EXCEPTION 'synthetic journal guard'; END IF;
 DELETE FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id'='{purchase_id}';
 DELETE FROM compras WHERE id={purchase_id} AND evento_id={event_id} AND operacion_id={qop};
 DELETE FROM purchase_idempotency WHERE idempotency_key={qkey} AND operacion_id={qop};
END $$;
COMMIT;
""")
    _psql("eventos_db", "eventos_db", f"""
BEGIN;
DO $$ BEGIN
 IF (SELECT count(*) FROM eventos WHERE id={event_id} AND nombre={qname} AND stock=1) <> 1 THEN RAISE EXCEPTION 'synthetic event guard'; END IF;
 IF (SELECT count(*) FROM reservas_stock WHERE evento_id={event_id} AND operacion_id={qop} AND estado='confirmada' AND cantidad=1) <> 1 THEN RAISE EXCEPTION 'synthetic reservation guard'; END IF;
 IF EXISTS (SELECT 1 FROM reservas_stock WHERE evento_id={event_id} AND operacion_id<>{qop}) THEN RAISE EXCEPTION 'unexpected reservation dependency'; END IF;
 DELETE FROM reservas_stock WHERE evento_id={event_id} AND operacion_id={qop};
 DELETE FROM eventos WHERE id={event_id} AND nombre={qname} AND stock=1;
END $$;
COMMIT;
""")
    _psql("usuarios_db", "usuarios_db", f"""
BEGIN;
DO $$ BEGIN
 IF (SELECT count(*) FROM usuarios WHERE id={buyer['id']} AND email={qemail} AND role='comprador') <> 1 THEN RAISE EXCEPTION 'synthetic user guard'; END IF;
 IF EXISTS (SELECT 1 FROM refresh_sessions WHERE user_id={buyer['id']} AND revoked_at IS NULL) THEN RAISE EXCEPTION 'active synthetic session'; END IF;
 DELETE FROM refresh_sessions WHERE user_id={buyer['id']};
 DELETE FROM usuarios WHERE id={buyer['id']} AND email={qemail};
END $$;
COMMIT;
""")
    assert _snapshot(event_id, event_name, key) == {
        "eventos": {"event": None, "reservations": []},
        "compras": {"purchases": [], "claim": None, "commits": 0},
    }
    assert _psql("usuarios_db", "usuarios_db", f"SELECT count(*) FROM usuarios WHERE id={buyer['id']} AND email={qemail}") == "0"
    return {"event": 1, "reservation": 1, "purchase": 1, "claim": 1,
            "journal_commit": 1, "user": 1}


def test_purchase_retries_safely_after_events_unavailable_before_reservation():
    preflight = _preflight()
    run = uuid.uuid4().hex
    # PostgreSQL stores Evento.id as signed int32; keep synthetic IDs below its limit.
    event_id = 1_500_000_000 + uuid.uuid4().int % 400_000_000
    event_name = f"NEXUSTIX QA EVENTS OUTAGE {run}"
    key = f"events-down-{run}"
    session = requests.Session()
    buyer = None
    event_created = False
    events_stopped = False
    events_stopped_at = None
    events_restored_at = None
    outage_http = None
    outage_latency_ms = None
    try:
        assert _psql("eventos_db", "eventos_db", f"SELECT count(*) FROM eventos WHERE id={event_id} OR nombre='{event_name}'") == "0"
        assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM compras WHERE evento_id={event_id}") == "0"
        assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM purchase_idempotency WHERE idempotency_key='{key}'") == "0"
        inserted = _psql("eventos_db", "eventos_db", f"INSERT INTO eventos (id,nombre,stock) VALUES ({event_id},'{event_name}',2) RETURNING id")
        assert inserted == str(event_id)
        event_created = True
        buyer = _register_buyer(session, run)
        body = {"evento_id": event_id, "cantidad": 1}
        headers = {"Authorization": f"Bearer {buyer['token']}", "Idempotency-Key": key}
        initial = _snapshot(event_id, event_name, key)
        assert initial["eventos"] == {"event": {"id": event_id, "name": event_name, "stock": 2}, "reservations": []}
        assert initial["compras"] == {"purchases": [], "claim": None, "commits": 0}

        before_rows = _container_rows()
        stop = _compose("stop", "eventos", timeout=30)
        assert stop.returncode == 0, "no se pudo detener solo Eventos en staging"
        events_stopped = True
        events_stopped_at = time.monotonic()
        after_stop = _container_rows()
        assert after_stop["eventos"].get("State") != "running"
        for protected in ("usuarios", "compras1", "compras2", "compras_lb",
                          "usuarios_db", "eventos_db", "compras_db", "frontend"):
            assert after_stop[protected].get("State") == "running", f"se alteró servicio fuera de alcance: {protected}"
            assert after_stop[protected].get("ID") == before_rows[protected].get("ID"), f"cambió contenedor fuera de alcance: {protected}"

        start = time.perf_counter()
        failed = session.post(f"{LB}/compras", headers=headers, json=body, timeout=20)
        outage_latency_ms = round((time.perf_counter() - start) * 1000, 1)
        outage_http = failed.status_code
        assert failed.status_code == 503, f"compra sin Eventos produjo HTTP {failed.status_code}"
        detail = failed.json().get("detail", {})
        operation_id = detail.get("operacion_id")
        assert isinstance(operation_id, str) and operation_id
        during = _snapshot(event_id, event_name, key)
        assert during["compras"]["claim"]["operation_id"] == operation_id
        assert during["compras"]["claim"]["state"] == "processing"
        assert during["compras"]["purchases"] == []
        assert during["eventos"]["event"]["stock"] == 2
        assert during["eventos"]["reservations"] == []
    finally:
        if events_stopped:
            restored = _compose("start", "eventos", timeout=30)
            assert restored.returncode == 0, "no se pudo restaurar Eventos; se conservan los datos sintéticos"
            events_stopped = False

    deadline = time.monotonic() + 45
    while True:
        try:
            response = requests.get(f"{EVENTS}/health", timeout=3)
            if response.status_code == 200 and response.json().get("db") == "connected":
                events_restored_at = time.monotonic()
                break
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            pytest.fail("Eventos no recuperó health; se conservan los datos sintéticos")
        time.sleep(0.5)

    # Reuse exactly the subject, request body and idempotency key after restoration.
    success = session.post(f"{LB}/compras", headers=headers, json=body, timeout=20)
    assert success.status_code == 200, f"retry tras restaurar Eventos HTTP {success.status_code}"
    success_body = success.json()
    operation_id = success_body["operacion_id"]
    assert success_body.get("confirmacion_reserva") == "confirmada"
    state = _snapshot(event_id, event_name, key)
    assert state["eventos"] == {"event": {"id": event_id, "name": event_name, "stock": 1},
                                "reservations": [{"operation_id": operation_id, "quantity": 1, "state": "confirmada"}]}
    assert state["compras"]["claim"]["state"] == "completed"
    assert state["compras"]["claim"]["operation_id"] == operation_id
    assert len(state["compras"]["purchases"]) == 1
    assert state["compras"]["commits"] == 1
    replay = session.post(f"{LB}/compras", headers=headers, json=body, timeout=20)
    assert replay.status_code == 200 and replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json() == success_body
    assert _snapshot(event_id, event_name, key) == state

    logout = session.post(f"{USERS}/auth/logout", headers={
        "Origin": FRONTEND, "X-CSRF-Protection": "1",
    }, timeout=8)
    assert logout.status_code == 200
    cleaned = _cleanup(event_id, event_name, key, buyer, operation_id, int(success_body["id"]))
    event_created = False
    final_rows = _container_rows()
    assert all(final_rows[n].get("State") == "running" for n in final_rows)
    assert all(final_rows[n].get("ID") == before_rows[n].get("ID") for n in before_rows)
    print(json.dumps({
        "preflight": preflight,
        "scenario": "A_events_stopped_before_reservation",
        "initial": {"stock": 2, "claim": None, "purchases": 0, "reservations": 0},
        "during_outage_http": outage_http,
        "during_outage_latency_ms": outage_latency_ms,
        "events_unavailable_ms": round((events_restored_at - events_stopped_at) * 1000, 1),
        "during_outage": {"claim": "processing", "purchase_count": 0,
                          "reservation_count": 0, "stock": 2,
                          "operation_id_preserved": True},
        "retry_http": success.status_code,
        "replay_http": replay.status_code,
        "final_before_cleanup": {"stock": 1, "purchases": 1, "reservations_confirmed": 1,
                                  "claims_completed": 1, "journal_commits": 1},
        "cleanup": cleaned,
        "primary_environment_touched": False,
        "stopped_services": ["eventos"],
    }, sort_keys=True))

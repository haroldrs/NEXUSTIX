"""Controlled single-replica failover proof for nexustix-auth-staging only."""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "nexustix-auth-staging"
COMPOSE = ROOT / "docker-compose.staging.yml"
FRONTEND_URL = "http://localhost:18080"
USERS_URL = "http://localhost:18081"
EVENTS_URL = "http://localhost:18082"
PURCHASE1_URL = "http://localhost:18083"
PURCHASE2_URL = "http://localhost:18084"
LB_URL = "http://localhost:18085"
SERVICE_PORTS = {
    "frontend": 18080,
    "usuarios": 18081,
    "eventos": 18082,
    "compras1": 18083,
    "compras2": 18084,
    "compras_lb": 18085,
}
pytestmark = pytest.mark.skipif(
    os.getenv("NEXUSTIX_STAGING_HTTP") != "1",
    reason="Requiere habilitación explícita del staging aislado",
)


def _env():
    secrets_dir = os.getenv("NEXUSTIX_STAGING_SECRETS_DIR")
    assert secrets_dir, "Defina NEXUSTIX_STAGING_SECRETS_DIR para el staging aislado"
    resolved = Path(secrets_dir).expanduser().resolve()
    assert resolved.is_dir() and os.access(resolved, os.R_OK)
    result = os.environ.copy()
    result["NEXUSTIX_STAGING_SECRETS_DIR"] = str(resolved)
    return result


def _compose(*args, timeout=30):
    return subprocess.run(
        ["docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE), *args],
        cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=timeout,
    )


def _psql(service, database, sql):
    assert service in {"usuarios_db", "eventos_db", "compras_db"}
    result = _compose(
        "exec", "-T", service, "psql", "-U", "postgres", "-d", database,
        "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql,
        timeout=20,
    )
    assert result.returncode == 0, f"consulta de staging falló en {service} (código {result.returncode})"
    return result.stdout.strip()


def _preflight():
    urls = {
        USERS_URL: 18081, EVENTS_URL: 18082, PURCHASE1_URL: 18083,
        PURCHASE2_URL: 18084, LB_URL: 18085,
    }
    assert all(
        urlparse(url).scheme == "http"
        and urlparse(url).hostname in {"localhost", "127.0.0.1"}
        and urlparse(url).port == port
        for url, port in urls.items()
    ), "se rechazó URL fuera de los puertos locales de staging"

    config = _compose("config", "--quiet")
    assert config.returncode == 0, "Compose staging config --quiet falló (salida sensible omitida)"
    listed = _compose("ps", "--all", "--format", "json")
    assert listed.returncode == 0, "no se pudo consultar el estado del proyecto staging"
    containers = [json.loads(line) for line in listed.stdout.splitlines() if line.strip()]
    required = {
        "frontend", "usuarios", "eventos", "compras1", "compras2", "compras_lb",
        "usuarios_db", "eventos_db", "compras_db",
    }
    assert len(containers) == 9
    assert {row.get("Service") for row in containers} == required
    assert all(row.get("Project") == PROJECT for row in containers)
    assert all(row.get("State") == "running" for row in containers)
    for row in containers:
        service = row["Service"]
        if service in SERVICE_PORTS:
            ports = {pub.get("PublishedPort") for pub in row.get("Publishers", []) if pub.get("PublishedPort")}
            assert ports == {SERVICE_PORTS[service]}, f"binding de staging inesperado para {service}"
        if service.endswith("_db"):
            assert row.get("Health") == "healthy", f"base staging no saludable: {service}"

    revisions = {
        "users": _psql("usuarios_db", "usuarios_db", "SELECT version_num FROM alembic_version"),
        "purchases": _psql("compras_db", "compras_db", "SELECT version_num FROM alembic_version"),
    }
    assert revisions == {"users": "20261022_01", "purchases": "20261023_02"}

    for url in (FRONTEND_URL, USERS_URL, EVENTS_URL, PURCHASE1_URL, PURCHASE2_URL, LB_URL):
        path = "/" if url == FRONTEND_URL else "/health"
        response = requests.get(f"{url}{path}", timeout=8)
        assert response.status_code == 200, f"staging HTTP no saludable en puerto {urlparse(url).port}"
        if url != FRONTEND_URL:
            assert response.json().get("db") == "connected", f"DB desconectada en puerto {urlparse(url).port}"


def _new_buyer(session, suffix):
    email = f"qa-failover-{suffix}@example.com"
    password = secrets.token_urlsafe(28)
    response = session.post(
        f"{USERS_URL}/auth/register",
        json={"nombre": "Comprador failover QA", "email": email, "password": password},
        timeout=10,
    )
    assert response.status_code == 201, f"registro sintético HTTP {response.status_code}"
    payload = response.json()
    assert payload.get("user", {}).get("role") == "comprador"
    assert payload.get("user", {}).get("email") == email
    token = payload.get("access_token")
    assert isinstance(token, str) and token
    claims_part = token.split(".")[1]
    claims_part += "=" * (-len(claims_part) % 4)
    claims = json.loads(base64.urlsafe_b64decode(claims_part.encode("ascii")))
    assert claims.get("role") == "comprador" and str(claims.get("sub")) == str(payload["user"]["id"])
    return {
        "email": email,
        "user_id": payload["user"]["id"],
        "token": token,
        "subject": f"{claims['iss']}|{claims['sub']}",
    }


def _snapshot(event_id, keys):
    key_list = ",".join("'" + key + "'" for key in keys)
    event_raw = _psql(
        "eventos_db", "eventos_db",
        f"SELECT json_build_object('event',json_build_object('id',e.id,'name',e.nombre,'stock',e.stock),'reservations',coalesce(json_agg(json_build_object('operation_id',r.operacion_id,'quantity',r.cantidad,'state',r.estado) ORDER BY r.operacion_id) FILTER (WHERE r.operacion_id IS NOT NULL),'[]'::json)) FROM eventos e LEFT JOIN reservas_stock r ON r.evento_id=e.id WHERE e.id={event_id} GROUP BY e.id",
    )
    purchases = json.loads(_psql(
        "compras_db", "compras_db",
        f"SELECT json_build_object('purchases',coalesce((SELECT json_agg(json_build_object('id',c.id,'user_id',c.usuario_id,'subject',c.auth_subject,'event_id',c.evento_id,'quantity',c.cantidad,'state',c.estado,'operation_id',c.operacion_id) ORDER BY c.id) FROM compras c WHERE c.evento_id={event_id}),'[]'::json),'claims',coalesce((SELECT json_agg(json_build_object('key',i.idempotency_key,'operation_id',i.operacion_id,'subject',i.auth_subject,'state',i.state,'status',i.response_status,'body',i.response_body) ORDER BY i.idempotency_key) FROM purchase_idempotency i WHERE i.idempotency_key IN ({key_list})),'[]'::json),'journal_commits',(SELECT count(*) FROM journal_compras j WHERE j.tipo='COMMIT' AND j.detalles->>'compra_id' IN (SELECT id::text FROM compras WHERE evento_id={event_id})))",
    ))
    return {"eventos": json.loads(event_raw) if event_raw else None, "compras": purchases}


def _health(url):
    response = requests.get(f"{url}/health", timeout=8)
    return response, response.json() if response.headers.get("content-type", "").startswith("application/json") else {}


def _checked_cleanup(event_id, event_name, keys, operations, purchase_ids, buyer):
    key_list = ",".join("'" + key + "'" for key in keys)
    op_list = ",".join("'" + op + "'" for op in operations)
    id_list = ",".join(str(int(pid)) for pid in purchase_ids)
    email = "'" + buyer["email"].replace("'", "''") + "'"
    name = "'" + event_name.replace("'", "''") + "'"
    subject = "'" + buyer["subject"].replace("'", "''") + "'"

    before = _snapshot(event_id, keys)
    assert before["eventos"] == {
        "event": {"id": event_id, "name": event_name, "stock": 2},
        "reservations": [
            {"operation_id": op, "quantity": 1, "state": "confirmada"}
            for op in sorted(operations)
        ],
    }, "inventario/reservas no son inequívocos; se conserva el fixture"
    rows = before["compras"]["purchases"]
    assert len(rows) == 2 and {row["id"] for row in rows} == set(purchase_ids)
    assert all(
        row["user_id"] == buyer["user_id"] and row["subject"] == buyer["subject"]
        and row["event_id"] == event_id and row["quantity"] == 1
        and row["state"] == "confirmada" and row["operation_id"] in operations
        for row in rows
    ), "compras inesperadas; se conserva el fixture"
    claims = before["compras"]["claims"]
    assert len(claims) == 2 and {row["key"] for row in claims} == set(keys)
    assert all(
        row["operation_id"] in operations and row["subject"] == buyer["subject"]
        and row["state"] == "completed" and row["status"] == 200
        and row["body"].get("confirmacion_reserva") == "confirmada"
        for row in claims
    ), "claims inesperados; se conserva el fixture"
    assert before["compras"]["journal_commits"] == 2

    user_state = json.loads(_psql(
        "usuarios_db", "usuarios_db",
        f"SELECT json_build_object('users',count(DISTINCT u.id),'sessions',count(s.id),'active',count(s.id) FILTER (WHERE s.revoked_at IS NULL),'user_id',min(u.id),'email',min(u.email)) FROM usuarios u LEFT JOIN refresh_sessions s ON s.user_id=u.id WHERE u.id={buyer['user_id']} GROUP BY u.id",
    ))
    assert user_state == {
        "users": 1, "sessions": 1, "active": 0,
        "user_id": buyer["user_id"], "email": buyer["email"],
    }, "identidad/sesión inesperada; se conserva el fixture"
    assert _psql(
        "compras_db", "compras_db",
        f"SELECT count(*) FROM compras WHERE (auth_subject={subject} OR usuario_id={buyer['user_id']}) AND id NOT IN ({id_list})",
    ) == "0", "hay otras compras del comprador sintético; se conserva"
    assert _psql(
        "compras_db", "compras_db",
        f"SELECT count(*) FROM purchase_idempotency WHERE auth_subject={subject} AND idempotency_key NOT IN ({key_list})",
    ) == "0", "hay otros claims del comprador sintético; se conserva"

    _psql("compras_db", "compras_db", f"""
DO $$ BEGIN
  IF (SELECT count(*) FROM compras WHERE id IN ({id_list}) AND evento_id={event_id} AND cantidad=1 AND estado='confirmada' AND auth_subject={subject} AND operacion_id IN ({op_list})) <> 2 THEN RAISE EXCEPTION 'synthetic purchase guard'; END IF;
  IF (SELECT count(*) FROM purchase_idempotency WHERE idempotency_key IN ({key_list}) AND auth_subject={subject} AND state='completed') <> 2 THEN RAISE EXCEPTION 'synthetic claim guard'; END IF;
  IF (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id' IN ({','.join("'" + str(int(pid)) + "'" for pid in purchase_ids)})) <> 2 THEN RAISE EXCEPTION 'synthetic journal guard'; END IF;
  DELETE FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id' IN ({','.join("'" + str(int(pid)) + "'" for pid in purchase_ids)});
  DELETE FROM compras WHERE id IN ({id_list}) AND evento_id={event_id} AND operacion_id IN ({op_list});
  DELETE FROM purchase_idempotency WHERE idempotency_key IN ({key_list}) AND auth_subject={subject};
END $$;
""")
    _psql("eventos_db", "eventos_db", f"""
DO $$ BEGIN
  IF (SELECT count(*) FROM eventos WHERE id={event_id} AND nombre={name} AND stock=2) <> 1 THEN RAISE EXCEPTION 'synthetic event guard'; END IF;
  IF (SELECT count(*) FROM reservas_stock WHERE evento_id={event_id} AND operacion_id IN ({op_list}) AND cantidad=1 AND estado='confirmada') <> 2 THEN RAISE EXCEPTION 'synthetic reservation guard'; END IF;
  DELETE FROM reservas_stock WHERE evento_id={event_id} AND operacion_id IN ({op_list});
  DELETE FROM eventos WHERE id={event_id} AND nombre={name} AND stock=2;
END $$;
""")
    _psql("usuarios_db", "usuarios_db", f"""
DO $$ BEGIN
  IF (SELECT count(*) FROM usuarios WHERE id={buyer['user_id']} AND email={email} AND role='comprador' AND auth_status='active') <> 1 THEN RAISE EXCEPTION 'synthetic user guard'; END IF;
  IF EXISTS (SELECT 1 FROM refresh_sessions WHERE user_id={buyer['user_id']} AND revoked_at IS NULL) THEN RAISE EXCEPTION 'active synthetic session'; END IF;
  DELETE FROM refresh_sessions WHERE user_id={buyer['user_id']};
  DELETE FROM usuarios WHERE id={buyer['user_id']} AND email={email};
END $$;
""")
    after = _snapshot(event_id, keys)
    assert after == {"eventos": None, "compras": {"purchases": [], "claims": [], "journal_commits": 0}}
    assert _psql("usuarios_db", "usuarios_db", f"SELECT count(*) FROM usuarios WHERE id={buyer['user_id']} AND email={email}") == "0"
    assert _psql("usuarios_db", "usuarios_db", f"SELECT count(*) FROM refresh_sessions WHERE user_id={buyer['user_id']}") == "0"
    return before, after


def _run_single_replica_failover(stopped_service, surviving_service):
    """Baseline/replay, stop one named staging replica, then restore and verify routing."""
    _preflight()
    survivor_map = {
        "compras1": ("compras2", PURCHASE2_URL, "2"),
        "compras2": ("compras1", PURCHASE1_URL, "1"),
    }
    assert stopped_service in survivor_map
    expected_survivor, survivor_url, survivor_instance = survivor_map[stopped_service]
    assert surviving_service == expected_survivor

    suffix = uuid.uuid4().hex
    event_id = 2_000_000_000 + (uuid.uuid4().int % 100_000_000)
    event_name = f"NEXUSTIX QA FAILOVER {suffix}"
    keys = [f"qa-failover-{suffix}-baseline", f"qa-failover-{suffix}-outage"]
    buyer_session = requests.Session()
    buyer = None
    inserted_event = False
    replica_stopped = False
    stop_completed_at = None
    downtime_seconds = None
    purchase_ids = []
    operation_ids = []
    outage_http_status = None
    outage_replay_status = None
    outage_http_latency_ms = None
    outage_probe_statuses = []
    restore_health = {}
    lb_instances_after_restore = []

    try:
        assert _psql("eventos_db", "eventos_db", f"SELECT count(*) FROM eventos WHERE id={event_id} OR nombre='{event_name}'") == "0"
        assert _psql("eventos_db", "eventos_db", f"SELECT count(*) FROM reservas_stock WHERE evento_id={event_id}") == "0"
        assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM compras WHERE evento_id={event_id}") == "0"
        assert _psql("usuarios_db", "usuarios_db", f"SELECT count(*) FROM usuarios WHERE email LIKE 'qa-failover-{suffix}-%@example.com'") == "0"
        assert all(_psql("compras_db", "compras_db", f"SELECT count(*) FROM purchase_idempotency WHERE idempotency_key='{key}'") == "0" for key in keys)

        insert = _psql(
            "eventos_db", "eventos_db",
            f"INSERT INTO eventos (id,nombre,stock) VALUES ({event_id},'{event_name}',4) RETURNING id",
        )
        assert insert == str(event_id)
        inserted_event = True
        assert _snapshot(event_id, keys) == {
            "eventos": {"event": {"id": event_id, "name": event_name, "stock": 4}, "reservations": []},
            "compras": {"purchases": [], "claims": [], "journal_commits": 0},
        }
        print(f"synthetic_failover_event={event_id} fixture_stock=4; event fixture inserted directly in staging PostgreSQL")

        buyer = _new_buyer(buyer_session, suffix)
        auth = {"Authorization": f"Bearer {buyer['token']}"}
        body = {"evento_id": event_id, "cantidad": 1}

        # Baseline purchase and replay while both replicas are running.
        before_http = requests.get(f"{EVENTS_URL}/eventos/{event_id}/stock", timeout=8)
        assert before_http.status_code == 200 and before_http.json()["stock"] == 4
        base_headers = {**auth, "Idempotency-Key": keys[0]}
        baseline = buyer_session.post(f"{LB_URL}/compras", headers=base_headers, json=body, timeout=20)
        assert baseline.status_code == 200, f"baseline HTTP {baseline.status_code}"
        baseline_body = baseline.json()
        assert baseline_body.get("confirmacion_reserva") == "confirmada"
        baseline_replay = buyer_session.post(f"{LB_URL}/compras", headers=base_headers, json=body, timeout=20)
        assert baseline_replay.status_code == 200 and baseline_replay.headers.get("Idempotency-Replayed") == "true"
        assert baseline_replay.json() == baseline_body
        purchase_ids.append(baseline_body["id"])
        operation_ids.append(baseline_body["operacion_id"])
        baseline_state = _snapshot(event_id, keys)
        assert baseline_state["eventos"] == {
            "event": {"id": event_id, "name": event_name, "stock": 3},
            "reservations": [{"operation_id": operation_ids[0], "quantity": 1, "state": "confirmada"}],
        }
        assert len(baseline_state["compras"]["purchases"]) == 1
        assert len(baseline_state["compras"]["claims"]) == 1
        assert baseline_state["compras"]["journal_commits"] == 1

        # Stop only the named staging service, retaining its container and volumes.
        replica_stopped = True
        stop_result = _compose("stop", stopped_service, timeout=30)
        assert stop_result.returncode == 0, f"Compose no pudo detener exclusivamente {stopped_service}"
        stop_completed_at = time.monotonic()
        stopped_ps = _compose("ps", "--all", "--format", "json")
        assert stopped_ps.returncode == 0
        rows = [json.loads(line) for line in stopped_ps.stdout.splitlines() if line.strip()]
        assert len(rows) == 9 and all(row.get("Project") == PROJECT for row in rows)
        by_service = {row["Service"]: row for row in rows}
        assert by_service[stopped_service].get("State") != "running"
        assert all(by_service[name].get("State") == "running" for name in {
            "frontend", "usuarios", "eventos", "compras2", "compras_lb",
            "usuarios_db", "eventos_db", "compras_db",
        } - {stopped_service})
        survivor, survivor_body = _health(survivor_url)
        assert survivor.status_code == 200 and survivor_body.get("instancia") == survivor_instance and survivor_body.get("db") == "connected"

        # Nginx may first attempt the stopped upstream. A GET is safe to probe;
        # the POST below is sent once, and never replayed automatically by this test.
        outage_instance = None
        for _ in range(4):
            probe_started = time.perf_counter()
            try:
                probe = requests.get(f"{LB_URL}/health", timeout=8)
                outage_probe_statuses.append({
                    "status": probe.status_code,
                    "latency_ms": round((time.perf_counter() - probe_started) * 1000, 1),
                })
                if probe.status_code == 200 and probe.json().get("instancia") == survivor_instance:
                    outage_instance = survivor_instance
                    break
            except requests.RequestException:
                outage_probe_statuses.append({"status": "transport_error", "latency_ms": round((time.perf_counter() - probe_started) * 1000, 1)})
        assert outage_instance == survivor_instance, "el balanceador no alcanzó la réplica superviviente"

        failover_headers = {**auth, "Idempotency-Key": keys[1]}
        request_started = time.perf_counter()
        outage_purchase = buyer_session.post(f"{LB_URL}/compras", headers=failover_headers, json=body, timeout=25)
        outage_http_latency_ms = round((time.perf_counter() - request_started) * 1000, 1)
        outage_http_status = outage_purchase.status_code
        assert outage_purchase.status_code == 200, f"compra durante caída HTTP {outage_purchase.status_code}"
        outage_body = outage_purchase.json()
        assert outage_body.get("confirmacion_reserva") == "confirmada"
        purchase_ids.append(outage_body["id"])
        operation_ids.append(outage_body["operacion_id"])

        # The explicit retry has the same key and body; only this call may replay.
        outage_replay = buyer_session.post(f"{LB_URL}/compras", headers=failover_headers, json=body, timeout=20)
        outage_replay_status = outage_replay.status_code
        assert outage_replay.status_code == 200
        assert outage_replay.headers.get("Idempotency-Replayed") == "true"
        assert outage_replay.json() == outage_body
        outage_state = _snapshot(event_id, keys)
        assert outage_state["eventos"] == {
            "event": {"id": event_id, "name": event_name, "stock": 2},
            "reservations": [
                {"operation_id": op, "quantity": 1, "state": "confirmada"}
                for op in sorted(operation_ids)
            ],
        }
        assert len(outage_state["compras"]["purchases"]) == 2
        assert len(outage_state["compras"]["claims"]) == 2
        assert outage_state["compras"]["journal_commits"] == 2

    finally:
        if replica_stopped:
            start_result = _compose("start", stopped_service, timeout=30)
            if start_result.returncode != 0:
                raise AssertionError(f"No se pudo restaurar {stopped_service}; se conservan los datos sintéticos para inspección")
            downtime_seconds = round(time.monotonic() - stop_completed_at, 3)
            replica_stopped = False

    # Verify both instances and that Nginx resumes distributing after fail_timeout.
    for name, url, instance in (("compras1", PURCHASE1_URL, "1"), ("compras2", PURCHASE2_URL, "2")):
        deadline = time.monotonic() + 20
        while True:
            try:
                response, payload = _health(url)
                if response.status_code == 200 and payload.get("instancia") == instance and payload.get("db") == "connected":
                    restore_health[name] = response.status_code
                    break
            except requests.RequestException:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError(f"{name} no recuperó health HTTP; fixture retenido")
            time.sleep(0.5)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and set(lb_instances_after_restore) != {"1", "2"}:
        try:
            response = requests.get(f"{LB_URL}/health", timeout=8)
            if response.status_code == 200:
                instance = str(response.json().get("instancia"))
                if instance in {"1", "2"}:
                    lb_instances_after_restore.append(instance)
        except requests.RequestException:
            pass
        if set(lb_instances_after_restore) != {"1", "2"}:
            time.sleep(0.5)
    assert set(lb_instances_after_restore) == {"1", "2"}, "Nginx no volvió a distribuir a ambas réplicas; fixture retenido"

    # Final state must be unchanged by restart and both keys must still replay.
    final_state = _snapshot(event_id, keys)
    assert final_state == outage_state
    for index in range(2):
        headers = {**auth, "Idempotency-Key": keys[index]}
        replay = buyer_session.post(f"{LB_URL}/compras", headers=headers, json=body, timeout=20)
        assert replay.status_code == 200 and replay.headers.get("Idempotency-Replayed") == "true"
        assert replay.json()["id"] == purchase_ids[index]
        assert replay.json()["operacion_id"] == operation_ids[index]
    after_replays = _snapshot(event_id, keys)
    assert after_replays == final_state

    logout = buyer_session.post(
        f"{USERS_URL}/auth/logout",
        headers={"Origin": FRONTEND_URL, "X-CSRF-Protection": "1"}, timeout=8,
    )
    assert logout.status_code == 200
    _, cleaned = _checked_cleanup(event_id, event_name, keys, operation_ids, purchase_ids, buyer)
    inserted_event = False

    print(json.dumps({
        "baseline_http": baseline.status_code,
        "baseline_replay_http": baseline_replay.status_code,
        "during_outage_probe_http": outage_probe_statuses,
        "during_outage_purchase_http": outage_http_status,
        "during_outage_purchase_latency_ms": outage_http_latency_ms,
        "during_outage_replay_http": outage_replay_status,
        "stopped_service": stopped_service,
        "surviving_service": surviving_service,
        "stopped_seconds": downtime_seconds,
        "health_after_restore": restore_health,
        "load_balancer_instances_after_restore": lb_instances_after_restore,
        "purchase_ids": purchase_ids,
        "operation_ids": operation_ids,
        "stock": "4 -> 3 -> 2; unchanged after replay and restart",
        "counts_before_cleanup": {"purchases": 2, "claims": 2, "reservations": 2, "journal_commits": 2},
        "cleanup_verified": cleaned == {"eventos": None, "compras": {"purchases": [], "claims": [], "journal_commits": 0}},
        "event_id": event_id,
    }, sort_keys=True))


def test_http_purchase_continues_when_compras1_stopped():
    """Stop only compras1; compras2 must serve the load-balanced purchase."""
    _run_single_replica_failover("compras1", "compras2")


def test_http_purchase_continues_when_compras2_stopped():
    """Stop only compras2; compras1 must serve the load-balanced purchase."""
    _run_single_replica_failover("compras2", "compras1")

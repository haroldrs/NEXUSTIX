"""Deterministic process-crash windows against isolated staging only."""

from __future__ import annotations

import base64
import concurrent.futures
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


def _env(crash_hooks: bool | None = None, restart_policy: str | None = None) -> dict[str, str]:
    secrets_dir = os.getenv("NEXUSTIX_STAGING_SECRETS_DIR")
    assert secrets_dir and Path(secrets_dir).expanduser().is_dir(), "faltan secretos de staging"
    result = os.environ.copy()
    result["NEXUSTIX_STAGING_SECRETS_DIR"] = str(Path(secrets_dir).expanduser().resolve())
    result["NEXUSTIX_TEST_CRASH_HOOKS"] = "1" if crash_hooks else "0"
    result["NEXUSTIX_STAGING_COMPRAS_RESTART_POLICY"] = restart_policy or "unless-stopped"
    return result


def _compose(*args: str, crash_hooks: bool | None = None,
             restart_policy: str | None = None, timeout: int = 60):
    return subprocess.run(
        ["docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE), *args],
        cwd=ROOT, env=_env(crash_hooks, restart_policy), capture_output=True, text=True, timeout=timeout,
    )


def _psql(service: str, db: str, sql: str) -> str:
    assert service in {"usuarios_db", "eventos_db", "compras_db"}
    result = _compose("exec", "-T", service, "psql", "-U", "postgres", "-d", db,
                      "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql, timeout=20)
    assert result.returncode == 0, f"consulta staging falló en {service} (código {result.returncode})"
    return result.stdout.strip()


def _preflight() -> None:
    config = _compose("config", "--quiet")
    assert config.returncode == 0, "configuración Compose staging inválida (salida omitida)"
    listed = _compose("ps", "--all", "--format", "json")
    assert listed.returncode == 0
    rows = [json.loads(line) for line in listed.stdout.splitlines() if line.strip()]
    required = set(PORTS) | {"usuarios_db", "eventos_db", "compras_db"}
    assert len(rows) == 9 and {r.get("Service") for r in rows} == required
    assert all(r.get("Project") == PROJECT and r.get("State") == "running" for r in rows)
    for row in rows:
        service = row["Service"]
        if service in PORTS:
            published = {p.get("PublishedPort") for p in row.get("Publishers", []) if p.get("PublishedPort")}
            assert published == {PORTS[service]}, f"puerto staging inesperado: {service}"
        if service.endswith("_db"):
            assert row.get("Health") == "healthy", f"DB staging no saludable: {service}"
    assert _psql("usuarios_db", "usuarios_db", "SELECT version_num FROM alembic_version") == "20261022_01"
    assert _psql("compras_db", "compras_db", "SELECT version_num FROM alembic_version") == "20261023_02"
    for url in (USERS, EVENTS, PURCHASE1, PURCHASE2, LB):
        response = requests.get(f"{url}/health", timeout=8)
        assert response.status_code == 200 and response.json().get("db") == "connected"
    assert requests.get(FRONTEND, timeout=8).status_code == 200


def _wait_health(url: str, instance: str | None = None, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{url}/health", timeout=3)
            body = response.json()
            if response.status_code == 200 and body.get("db") == "connected" and (instance is None or str(body.get("instancia")) == instance):
                return
        except requests.RequestException:
            pass
        time.sleep(0.4)
    raise AssertionError("la instancia staging no recuperó health antes del timeout")


def _container_rows() -> dict[str, dict]:
    result = _compose("ps", "--all", "--format", "json")
    assert result.returncode == 0
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(rows) == 9 and all(row.get("Project") == PROJECT for row in rows)
    return {row["Service"]: row for row in rows}


def _restart_count(container_id: str) -> int:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.RestartCount}}", container_id],
        cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, "no se pudo consultar RestartCount del contenedor staging"
    return int(result.stdout.strip())


def _exit_code(container_id: str) -> int:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.ExitCode}}", container_id],
        cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, "no se pudo consultar el código de salida del contenedor staging"
    return int(result.stdout.strip())


def _restart_policy(container_id: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", container_id],
        cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, "no se pudo consultar la política de reinicio staging"
    return result.stdout.strip()


def _assert_runtime_hook(service: str, instance: str, enabled: bool) -> None:
    result = _compose("exec", "-T", service, "python", "-c",
                      "import os; print('|'.join(os.getenv(k, '') for k in "
                      "('INSTANCE_ID','NEXUSTIX_DEPLOYMENT_ENV','NEXUSTIX_TEST_CRASH_HOOKS')))")
    assert result.returncode == 0, f"no se pudo verificar la configuración del hook en {service}"
    expected = f"{instance}|staging|{'1' if enabled else '0'}"
    assert result.stdout.strip() == expected, f"el hook no tiene el estado esperado en {service}"


def _assert_runtime_hooks_disabled() -> None:
    _assert_runtime_hook("compras1", "1", False)
    _assert_runtime_hook("compras2", "2", False)


@pytest.fixture(params=["compras1", "compras2"])
def crash_hook_service(request):
    service = request.param
    _preflight()
    _assert_runtime_hooks_disabled()
    activate = _compose("up", "-d", "--no-deps", "--force-recreate", service,
                        crash_hooks=True, restart_policy="no")
    assert activate.returncode == 0, f"no se pudo activar el hook de prueba solo en {service}"
    try:
        _assert_runtime_hook(service, "1" if service == "compras1" else "2", True)
        _wait_health(PURCHASE1 if service == "compras1" else PURCHASE2,
                     "1" if service == "compras1" else "2")
        yield service
    finally:
        # Recreate only the selected replica with the hook disabled.
        restore = _compose("up", "-d", "--no-deps", "--force-recreate", service, crash_hooks=False)
        assert restore.returncode == 0, f"no se pudo restaurar {service} con el hook apagado"
        _wait_health(PURCHASE1 if service == "compras1" else PURCHASE2,
                     "1" if service == "compras1" else "2")
        _assert_runtime_hooks_disabled()
        rows = _container_rows()
        assert _restart_policy(rows[service]["ID"]) == "unless-stopped"


def _register_buyer(session: requests.Session, suffix: str) -> dict:
    email = f"qa-crash-{suffix}@example.com"
    password = secrets.token_urlsafe(28)
    response = session.post(f"{USERS}/auth/register", json={
        "nombre": "Comprador crash QA", "email": email, "password": password,
    }, timeout=10)
    assert response.status_code == 201, f"registro sintético HTTP {response.status_code}"
    data = response.json()
    assert data.get("user", {}).get("role") == "comprador"
    claims_part = data["access_token"].split(".")[1]
    claims_part += "=" * (-len(claims_part) % 4)
    claims = json.loads(base64.urlsafe_b64decode(claims_part.encode("ascii")))
    return {"email": email, "user_id": data["user"]["id"], "token": data["access_token"],
            "subject": f"{claims['iss']}|{claims['sub']}"}


def _snapshot(event_id: int, key: str) -> dict:
    key_sql = "'" + key.replace("'", "''") + "'"
    raw = _psql("eventos_db", "eventos_db", f"""
SELECT json_build_object(
 'event', (SELECT json_build_object('id',id,'name',nombre,'stock',stock) FROM eventos WHERE id={event_id}),
 'reservations', coalesce((SELECT json_agg(json_build_object('operation_id',operacion_id,'quantity',cantidad,'state',estado) ORDER BY operacion_id) FROM reservas_stock WHERE evento_id={event_id}),'[]'::json)
)::text
""")
    events = json.loads(raw)
    purchase_raw = _psql("compras_db", "compras_db", f"""
SELECT json_build_object(
 'purchases', coalesce((SELECT json_agg(json_build_object('id',id,'event_id',evento_id,'quantity',cantidad,'state',estado,'operation_id',operacion_id) ORDER BY id) FROM compras WHERE evento_id={event_id}),'[]'::json),
 'claim', (SELECT json_build_object('key',idempotency_key,'operation_id',operacion_id,'state',state,'status',response_status,'confirmation',response_body->>'confirmacion_reserva') FROM purchase_idempotency WHERE idempotency_key={key_sql}),
 'commits', (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id' IN (SELECT id::text FROM compras WHERE evento_id={event_id}))
)::text
""")
    return {"eventos": events, "compras": json.loads(purchase_raw)}


def _crash_request(url: str, headers: dict, body: dict, checkpoint: str) -> str:
    crashed = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(requests.post, f"{url}/compras", headers={
            **headers, "X-NEXUSTIX-Test-Crash-At": checkpoint,
        }, json=body, timeout=18)
        try:
            response = future.result(timeout=20)
            raise AssertionError(f"failpoint no terminó el proceso; HTTP {response.status_code}")
        except requests.RequestException as exc:
            crashed = type(exc).__name__
    # Ensure the request thread is gone before checking process restart.
    assert crashed, "el proceso objetivo de Compras no terminó abruptamente"
    return crashed


def _cleanup(event_id: int, event_name: str, key: str, buyer: dict, purchase_id: int, operation_id: str) -> None:
    key_sql = "'" + key.replace("'", "''") + "'"
    op_sql = "'" + operation_id.replace("'", "''") + "'"
    name_sql = "'" + event_name.replace("'", "''") + "'"
    email_sql = "'" + buyer["email"].replace("'", "''") + "'"
    subject_sql = "'" + buyer["subject"].replace("'", "''") + "'"
    state = _snapshot(event_id, key)
    assert state["eventos"] == {
        "event": {"id": event_id, "name": event_name, "stock": 1},
        "reservations": [{"operation_id": operation_id, "quantity": 1, "state": "confirmada"}],
    }, "estado no inequívoco; no se limpia el fixture"
    assert state["compras"]["purchases"] == [{
        "id": purchase_id, "event_id": event_id, "quantity": 1,
        "state": "confirmada", "operation_id": operation_id,
    }], "compra inesperada; no se limpia el fixture"
    claim = state["compras"]["claim"]
    assert claim and claim["key"] == key and claim["operation_id"] == operation_id
    assert claim["state"] == "completed" and claim["status"] == 200 and claim["confirmation"] == "confirmada"
    assert state["compras"]["commits"] == 1
    assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM compras WHERE (auth_subject={subject_sql} OR usuario_id={buyer['user_id']}) AND id<>{purchase_id}") == "0"
    assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM purchase_idempotency WHERE auth_subject={subject_sql} AND idempotency_key<>{key_sql}") == "0"
    _psql("compras_db", "compras_db", f"""
DO $$ BEGIN
 IF (SELECT count(*) FROM compras WHERE id={purchase_id} AND evento_id={event_id} AND cantidad=1 AND estado='confirmada' AND auth_subject={subject_sql} AND operacion_id={op_sql}) <> 1 THEN RAISE EXCEPTION 'purchase guard'; END IF;
 IF (SELECT count(*) FROM purchase_idempotency WHERE idempotency_key={key_sql} AND auth_subject={subject_sql} AND operacion_id={op_sql} AND state='completed') <> 1 THEN RAISE EXCEPTION 'claim guard'; END IF;
 IF (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id'='{purchase_id}') <> 1 THEN RAISE EXCEPTION 'journal guard'; END IF;
 DELETE FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id'='{purchase_id}';
 DELETE FROM compras WHERE id={purchase_id} AND evento_id={event_id} AND operacion_id={op_sql};
 DELETE FROM purchase_idempotency WHERE idempotency_key={key_sql} AND auth_subject={subject_sql};
END $$;
""")
    _psql("eventos_db", "eventos_db", f"""
DO $$ BEGIN
 IF (SELECT count(*) FROM eventos WHERE id={event_id} AND nombre={name_sql} AND stock=1) <> 1 THEN RAISE EXCEPTION 'event guard'; END IF;
 IF (SELECT count(*) FROM reservas_stock WHERE evento_id={event_id} AND operacion_id={op_sql} AND cantidad=1 AND estado='confirmada') <> 1 THEN RAISE EXCEPTION 'reservation guard'; END IF;
 DELETE FROM reservas_stock WHERE evento_id={event_id} AND operacion_id={op_sql};
 DELETE FROM eventos WHERE id={event_id} AND nombre={name_sql} AND stock=1;
END $$;
""")
    _psql("usuarios_db", "usuarios_db", f"""
DO $$ BEGIN
 IF (SELECT count(*) FROM usuarios WHERE id={buyer['user_id']} AND email={email_sql} AND role='comprador' AND auth_status='active') <> 1 THEN RAISE EXCEPTION 'user guard'; END IF;
 IF EXISTS (SELECT 1 FROM refresh_sessions WHERE user_id={buyer['user_id']} AND revoked_at IS NULL) THEN RAISE EXCEPTION 'active session'; END IF;
 DELETE FROM refresh_sessions WHERE user_id={buyer['user_id']};
 DELETE FROM usuarios WHERE id={buyer['user_id']} AND email={email_sql};
END $$;
""")
    assert _snapshot(event_id, key) == {"eventos": {"event": None, "reservations": []},
                                        "compras": {"purchases": [], "claim": None, "commits": 0}}


@pytest.mark.parametrize("scenario,checkpoint", [
    ("before_claim", "before_claim"),
    ("after_claim_before_reservation", "after_claim"),
    ("after_reservation_before_purchase", "after_reservation"),
    ("purchase_and_response_persisted_before_http_reply", "after_final_response_commit"),
])
def test_abrupt_purchase_process_crash_recovers_idempotently(crash_hook_service, scenario, checkpoint):
    target_service = crash_hook_service
    target_url = PURCHASE1 if target_service == "compras1" else PURCHASE2
    target_instance = "1" if target_service == "compras1" else "2"
    survivor_service = "compras2" if target_service == "compras1" else "compras1"
    survivor_url = PURCHASE2 if survivor_service == "compras2" else PURCHASE1
    survivor_instance = "2" if survivor_service == "compras2" else "1"
    _preflight()
    suffix = uuid.uuid4().hex
    event_id = 1_500_000_000 + secrets.randbelow(400_000_000)
    event_name = f"NEXUSTIX QA ABRUPT CRASH {suffix}"
    key = f"qa-abrupt-crash-{suffix}"
    buyer_session = requests.Session()
    buyer = None
    fixture_inserted = False
    try:
        assert _psql("eventos_db", "eventos_db", f"SELECT count(*) FROM eventos WHERE id={event_id} OR nombre='{event_name}'") == "0"
        assert _psql("compras_db", "compras_db", f"SELECT count(*) FROM purchase_idempotency WHERE idempotency_key='{key}'") == "0"
        _psql("eventos_db", "eventos_db", f"INSERT INTO eventos(id,nombre,stock) VALUES ({event_id},'{event_name}',2)")
        fixture_inserted = True
        buyer = _register_buyer(buyer_session, suffix)
        headers = {"Authorization": f"Bearer {buyer['token']}", "Idempotency-Key": key}
        body = {"evento_id": event_id, "cantidad": 1}
        _wait_health(target_url, target_instance)
        _wait_health(survivor_url, survivor_instance)
        containers_before = _container_rows()
        before_ids = {name: row["ID"] for name, row in containers_before.items()}
        restart_count_before = _restart_count(before_ids[target_service])
        survivor_restart_count_before = _restart_count(before_ids[survivor_service])
        assert _restart_policy(before_ids[target_service]) == "no"
        started = time.monotonic()
        transport_error = _crash_request(target_url, headers, body, checkpoint)
        crashed_state = _snapshot(event_id, key)

        expected = {
            "before_claim": (None, 0, None, 2),
            "after_claim": ("processing", 0, None, 2),
            "after_reservation": ("processing", 0, "reservada", 1),
            "after_final_response_commit": ("completed", 1, "confirmada", 1),
        }[checkpoint]
        stopped_rows = _container_rows()
        assert stopped_rows[target_service]["ID"] == before_ids[target_service]
        assert stopped_rows[target_service]["State"] != "running", (
            f"{target_service} no permaneció detenido tras la terminación abrupta"
        )
        assert _exit_code(before_ids[target_service]) == 86, "el proceso no terminó por el hook esperado"
        assert stopped_rows[survivor_service]["State"] == "running"
        _wait_health(survivor_url, survivor_instance)
        lb_health = requests.get(f"{LB}/health", timeout=8)
        assert lb_health.status_code == 200 and str(lb_health.json().get("instancia")) == survivor_instance, (
            "el balanceador no atendió health desde la réplica superviviente mientras el objetivo estaba detenido"
        )

        claim = crashed_state["compras"]["claim"]
        observed = (
            claim["state"] if claim else None,
            len(crashed_state["compras"]["purchases"]),
            crashed_state["eventos"]["reservations"][0]["state"] if crashed_state["eventos"]["reservations"] else None,
            crashed_state["eventos"]["event"]["stock"],
        )
        assert observed == expected, f"checkpoint persistido distinto al esperado para {scenario}"
        if checkpoint == "after_final_response_commit":
            assert claim["confirmation"] == "confirmada" and crashed_state["compras"]["commits"] == 1
        old_operation_id = claim["operation_id"] if claim else None

        # The target is intentionally held stopped so the load balancer must use
        # the survivor. Its request is safe because key, subject and payload are identical.
        retry = buyer_session.post(f"{LB}/compras", headers=headers, json=body, timeout=20)
        assert retry.status_code == 200, f"same-key retry por el balanceador HTTP {retry.status_code}"
        result = retry.json()
        assert result.get("confirmacion_reserva") == "confirmada"
        assert result.get("evento_id") == event_id and result.get("cantidad") == 1
        operation_id = result["operacion_id"]
        if old_operation_id is not None:
            assert operation_id == old_operation_id, "retry cambió el operacion_id durable"
        survivor_retry = buyer_session.post(f"{survivor_url}/compras", headers=headers,
                                            json=body, timeout=20)
        assert survivor_retry.status_code == 200 and survivor_retry.headers.get("Idempotency-Replayed") == "true"
        assert survivor_retry.json()["operacion_id"] == operation_id
        replay = buyer_session.post(f"{LB}/compras", headers=headers, json=body, timeout=20)
        assert replay.status_code == 200 and replay.headers.get("Idempotency-Replayed") == "true"
        assert replay.json()["id"] == result["id"] and replay.json()["operacion_id"] == operation_id

        restart_count_after = _restart_count(before_ids[target_service])
        survivor_restart_count_after = _restart_count(before_ids[survivor_service])
        assert restart_count_after == restart_count_before, (
            f"el reinicio automático de {target_service} no quedó aislado durante la verificación"
        )
        assert survivor_restart_count_after == survivor_restart_count_before, (
            f"la réplica superviviente {survivor_service} también se reinició"
        )
        final = _snapshot(event_id, key)
        assert final["eventos"] == {
            "event": {"id": event_id, "name": event_name, "stock": 1},
            "reservations": [{"operation_id": operation_id, "quantity": 1, "state": "confirmada"}],
        }
        assert len(final["compras"]["purchases"]) == 1
        assert len(final["compras"]["purchases"]) == len(final["eventos"]["reservations"]) == 1
        assert final["compras"]["claim"]["state"] == "completed"
        assert final["compras"]["claim"]["confirmation"] == "confirmada"
        assert final["compras"]["commits"] == 1

        logout = buyer_session.post(f"{USERS}/auth/logout", headers={
            "Origin": FRONTEND, "X-CSRF-Protection": "1",
        }, timeout=10)
        assert logout.status_code == 200
        _cleanup(event_id, event_name, key, buyer, result["id"], operation_id)
        fixture_inserted = False
        print(json.dumps({
            "scenario": scenario, "checkpoint": checkpoint,
            "target_service": target_service,
            "client_first_attempt": "transport_lost", "transport_exception_type": transport_error,
            "retry_http": retry.status_code, "replay_http": replay.status_code,
            "initial_state": observed, "final_stock": 1,
            "purchase_count": 1, "reservation_count": 1,
            "same_operation_id_after_crash": (
                "no_original_operation_id_before_claim" if old_operation_id is None
                else old_operation_id == operation_id
            ),
            "survivor_health_during_target_stop": 200,
            "target_exit_code": 86,
            "target_restart_count": f"{restart_count_before} -> {restart_count_after}",
            "survivor_restart_count": f"{survivor_restart_count_before} -> {survivor_restart_count_after}",
            "elapsed_s": round(time.monotonic() - started, 3),
            "cleanup_verified": True,
        }, sort_keys=True))
    finally:
        if buyer is not None:
            try:
                buyer_session.post(f"{USERS}/auth/logout", headers={
                    "Origin": FRONTEND, "X-CSRF-Protection": "1",
                }, timeout=8)
            except requests.RequestException:
                pass
        if fixture_inserted:
            # Deliberately retain the fixture if the final expected state is not proven.
            print(json.dumps({"fixture_retained_for_review": True, "event_id": event_id,
                              "scenario": scenario}, sort_keys=True))

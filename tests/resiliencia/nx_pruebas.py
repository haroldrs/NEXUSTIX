"""Ayudas para las pruebas de resiliencia sobre el entorno de PRUEBAS (nexustix-auth-staging).

Toda acción sobre contenedores localiza el contenedor por las etiquetas Compose
del proyecto de pruebas y vuelve a verificarlas antes de actuar; nunca se usan
nombres fijos ni se toca el entorno de demostración.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import entornos  # noqa: E402
import pg_docker  # noqa: E402

ENT = entornos.get("pruebas")
ENABLED = os.getenv("NEXUSTIX_STAGING_HTTP") == "1"
USERS = ENT.url("usuarios")
EVENTS = ENT.url("eventos")
R1 = ENT.url("compras1")
R2 = ENT.url("compras2")
LB = ENT.url("compras_lb")
FRONTEND = ENT.url("frontend")
REPLICA_URL = {"compras1": R1, "compras2": R2}
ADMIN_EMAIL = os.getenv("NEXUSTIX_STAGING_ADMIN_EMAIL", "qa-admin@example.com")


# ------------------------------------------------------------------ Docker
def compose(*args: str, env_extra: dict[str, str] | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    env = ENT.env()
    env.update(env_extra or {})
    return subprocess.run([*ENT.compose_args(ha=ha_active()), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=timeout)


def ha_active() -> bool:
    return any(entornos.containers(ENT, f"{db}_replica") for db in entornos.DATABASES)


def container(service: str) -> str:
    cid = entornos.container_id(ENT, service)
    entornos.verify_labels(ENT, cid, service)
    return cid


def inspect(cid: str, template: str) -> str:
    return entornos.inspect(cid, template)


def docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, f"docker {args[0]} falló: {result.stderr.strip()[-300:]}"
    return result


def kill(service: str) -> str:
    """SIGKILL del contenedor exacto del servicio (fallo abrupto; Docker no lo reinicia)."""
    cid = container(service)
    docker("kill", "--signal", "KILL", cid)
    wait_until(lambda: inspect(cid, "{{.State.Running}}") == "false", 20, f"{service} sigue en ejecución")
    return cid


def start(cid: str, service: str) -> None:
    entornos.verify_labels(ENT, cid, service)
    docker("start", cid)
    wait_container_healthy(service)


def wait_until(predicate, timeout: float, message: str, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - se reintenta hasta el plazo
            last_error = exc
        time.sleep(interval)
    raise AssertionError(f"{message} (último error: {last_error})")


def wait_container_healthy(service: str, timeout: float = 120) -> None:
    cid = container(service)
    wait_until(lambda: inspect(cid, "{{if .State.Health}}{{.State.Health.Status}}{{end}}") == "healthy",
               timeout, f"{service} no quedó healthy")


def logs_contain(cid: str, text: str, since: str) -> bool:
    result = subprocess.run(["docker", "logs", "--since", since, cid], capture_output=True, text=True, timeout=30)
    return text in (result.stdout + result.stderr)


def set_test_hooks(service: str, enabled: bool) -> None:
    """Recrea solo esa réplica con los ganchos de prueba activados o desactivados."""
    assert service in REPLICA_URL
    result = compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", service,
                     env_extra={"NEXUSTIX_TEST_CRASH_HOOKS": "1" if enabled else "0"})
    assert result.returncode == 0, f"no se pudo recrear {service}: {result.stderr.strip()[-400:]}"
    flag = compose("exec", "-T", service, "python", "-c", "import os; print(os.environ['NEXUSTIX_TEST_CRASH_HOOKS'])")
    assert flag.stdout.strip() == ("1" if enabled else "0")


# ------------------------------------------------------------------ PostgreSQL
def sql(db: str, statement: str) -> str:
    return pg_docker.psql(pg_docker.primario(ENT, db).container, db, statement)


def create_event(stock: int) -> int:
    event_id = int(uuid.uuid4().int % 1_900_000_000) + 10_000_000
    sql("eventos_db", f"INSERT INTO eventos (id, nombre, stock, stock_inicial) "
                      f"VALUES ({event_id}, 'QA resiliencia {event_id}', {stock}, {stock})")
    return event_id


def event_state(event_id: int) -> dict:
    eventos = json.loads(sql("eventos_db", f"""
SELECT json_build_object(
 'stock', (SELECT stock FROM eventos WHERE id = {event_id}),
 'reservas', coalesce((SELECT json_agg(json_build_object('op', operacion_id, 'estado', estado, 'cantidad', cantidad))
                       FROM reservas_stock WHERE evento_id = {event_id}), '[]'::json))::text"""))
    compras = json.loads(sql("compras_db", f"""
SELECT json_build_object(
 'compras', coalesce((SELECT json_agg(json_build_object('id', id, 'op', operacion_id, 'cantidad', cantidad))
                      FROM compras WHERE evento_id = {event_id}), '[]'::json))::text"""))
    return {**eventos, **compras}


def claim(key: str) -> dict | None:
    safe = key.replace("'", "''")
    raw = sql("compras_db", f"""
SELECT json_build_object('state', state, 'status', response_status, 'op', operacion_id,
  'confirmacion', response_body->>'confirmacion_reserva')::text
FROM purchase_idempotency WHERE idempotency_key = '{safe}'""")
    return json.loads(raw) if raw else None


def assert_single_consistent_purchase(event_id: int, key: str, initial_stock: int, quantity: int = 1) -> dict:
    state = event_state(event_id)
    record = claim(key)
    assert record and record["state"] == "completed" and record["status"] == 200, record
    purchases = [c for c in state["compras"] if c["op"] == record["op"]]
    assert len(purchases) == 1, f"se esperaba una compra para la operación: {state}"
    reservations = [r for r in state["reservas"] if r["op"] == record["op"]]
    assert [r["estado"] for r in reservations] == ["confirmada"], state
    assert state["stock"] == initial_stock - quantity, state
    return state


# ------------------------------------------------------------------ HTTP
def register_buyer() -> dict:
    email = f"qa-resiliencia-{uuid.uuid4().hex[:12]}@example.com"
    session = requests.Session()
    response = session.post(f"{USERS}/auth/register", json={"nombre": "QA resiliencia", "email": email,
                                                           "password": secrets.token_urlsafe(24)}, timeout=10)
    assert response.status_code == 201, response.text
    data = response.json()
    payload = data["access_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return {"email": email, "token": data["access_token"], "user_id": data["user"]["id"],
            "subject": f"{claims['iss']}|{claims['sub']}", "session": session}


def admin_token() -> str:
    password = (ENT.secrets_dir / "admin-password").read_text(encoding="utf-8").strip()
    response = requests.post(f"{USERS}/auth/login", json={"email": ADMIN_EMAIL, "password": password}, timeout=10)
    if response.status_code == 401:
        created = subprocess.run([sys.executable, str(ROOT / "scripts" / "nx.py"), "pruebas", "admin", "--email", ADMIN_EMAIL],
                                 cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert created.returncode == 0, created.stdout + created.stderr
        response = requests.post(f"{USERS}/auth/login", json={"email": ADMIN_EMAIL, "password": password}, timeout=10)
    assert response.status_code == 200 and response.json()["user"]["role"] == "administrador", response.text
    return response.json()["access_token"]


def purchase(token: str, event_id: int, key: str, *, url: str = LB, quantity: int = 1,
             headers: dict | None = None, timeout: float = 35) -> requests.Response:
    return requests.post(f"{url}/compras", json={"evento_id": event_id, "cantidad": quantity},
                         headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key, **(headers or {})},
                         timeout=timeout)


def purchase_until_done(token: str, event_id: int, key: str, timeout: float = 90) -> requests.Response:
    """Reintenta SIEMPRE con la misma clave mientras haya error de red o 5xx."""
    def attempt():
        try:
            response = purchase(token, event_id, key)
        except requests.RequestException:
            return None
        return response if response.status_code < 500 else None
    return wait_until(attempt, timeout, "la compra no se completó reintentando con la misma clave", interval=2)


def audit(admin: str) -> dict:
    response = requests.get(f"{LB}/auditoria/consistencia", headers={"Authorization": f"Bearer {admin}"}, timeout=30)
    assert response.status_code == 200, response.text
    return response.json()


def recover(admin: str) -> dict:
    response = requests.post(f"{LB}/compras/recuperar-servidor", headers={"Authorization": f"Bearer {admin}"}, timeout=60)
    assert response.status_code == 200, response.text
    return response.json()


def all_healthy() -> None:
    for service in ("usuarios", "eventos", "compras1", "compras2", "compras_lb", "frontend"):
        wait_container_healthy(service, timeout=180)


def demo_fingerprint() -> dict:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "nx.py"), "demo", "huella"],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    if result.returncode:
        return {"error": result.stderr.strip()[-300:]}
    return json.loads(result.stdout)

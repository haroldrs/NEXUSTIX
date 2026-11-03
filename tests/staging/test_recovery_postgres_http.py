"""End-to-end recovery proof against the disposable staging Compose project."""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path

import pytest
import requests


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "nexustix-auth-staging"
COMPOSE = ROOT / "docker-compose.staging.yml"
USERS_URL = "http://localhost:18081"
EXPECTED_REVISION = "20261023_02"
pytestmark = pytest.mark.skipif(
    os.getenv("NEXUSTIX_STAGING_HTTP") != "1",
    reason="Requiere staging aislado habilitado explícitamente",
)


def _compose(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE), *args]
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        env=os.environ.copy(),
    )


def _safe_run(command: list[str], timeout: int = 20) -> str:
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, f"falló comprobación de staging (código {result.returncode})"
    return result.stdout.strip()


def _psql(service: str, database: str, sql: str) -> str:
    assert service in {"usuarios_db", "eventos_db", "compras_db"}
    return _safe_run([
        "docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE),
        "exec", "-T", service, "psql", "-U", "postgres", "-d", database,
        "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ])


def _password_and_admin_token() -> tuple[requests.Session, str, int]:
    secrets_dir = os.environ.get("NEXUSTIX_STAGING_SECRETS_DIR")
    password_file = os.environ.get("NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE")
    email = os.environ.get("NEXUSTIX_STAGING_ADMIN_EMAIL")
    assert secrets_dir and Path(secrets_dir).expanduser().is_dir(), "faltan secretos de staging"
    assert password_file and Path(password_file).expanduser().is_file(), "falta archivo QA de staging"
    assert email == "qa-admin-staging@example.com", "la identidad no es el administrador sintético documentado"
    secret_path = Path(password_file).expanduser().resolve()
    secret_root = Path(secrets_dir).expanduser().resolve()
    assert secret_path.parent == secret_root, "el archivo QA no pertenece al directorio de secretos de staging"
    assert os.access(secret_path, os.R_OK), "el archivo QA de staging no es legible"
    mode = secret_path.stat().st_mode & 0o777
    assert mode & 0o077 == 0, "el archivo QA debe ser inaccesible para grupo y otros"

    password = secret_path.read_text(encoding="utf-8").strip()
    session = requests.Session()
    response = session.post(
        f"{USERS_URL}/auth/login", json={"email": email, "password": password}, timeout=10,
    )
    del password
    assert response.status_code == 200, f"login QA HTTP {response.status_code}"
    payload = response.json()
    assert payload.get("user", {}).get("role") == "administrador", "la cuenta QA no tiene rol administrador"
    token = payload.get("access_token")
    assert isinstance(token, str) and token, "login no entregó access token"
    assert int(payload.get("expires_in", 0)) > 180, "token insuficiente para esperar el umbral recovery"
    return session, token, int(payload["expires_in"])


def _logout(session: requests.Session) -> None:
    try:
        session.post(
            f"{USERS_URL}/auth/logout",
            headers={"Origin": "http://localhost:18080", "X-CSRF-Protection": "1"},
            timeout=10,
        )
    except requests.RequestException:
        pass


def _preflight() -> dict[str, str]:
    secrets_dir = os.environ.get("NEXUSTIX_STAGING_SECRETS_DIR")
    assert secrets_dir, "NEXUSTIX_STAGING_SECRETS_DIR debe apuntar a staging"
    env = os.environ.copy()
    env["NEXUSTIX_STAGING_SECRETS_DIR"] = str(Path(secrets_dir).expanduser())
    config = subprocess.run(
        ["docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE), "config", "--quiet"],
        cwd=ROOT, capture_output=True, text=True, timeout=30, env=env,
    )
    assert config.returncode == 0, f"validación silenciosa Compose falló (código {config.returncode})"

    running = _compose("ps", "--status", "running", "--services")
    assert running.returncode == 0, f"consulta de servicios staging falló (código {running.returncode})"
    services = set(running.stdout.split())
    required = {"usuarios_db", "eventos_db", "compras_db", "usuarios", "eventos",
                "compras1", "compras2", "compras_lb"}
    assert required <= services, "no están activos todos los servicios requeridos del proyecto de staging"

    images = _compose("config", "--images")
    assert images.returncode == 0, f"consulta de imágenes Compose falló (código {images.returncode})"
    assert all(name in images.stdout for name in (
        "nexustix-staging/usuarios:auth", "nexustix-staging/eventos:auth",
        "nexustix-staging/compras:auth",
    )), "Compose no resuelve imágenes de staging esperadas"

    purchases_revision = _psql("compras_db", "compras_db", "SELECT version_num FROM alembic_version")
    assert purchases_revision == EXPECTED_REVISION, "Compras staging no está en la revisión 20261023_02"
    users_revision = _psql("usuarios_db", "usuarios_db", "SELECT version_num FROM alembic_version")
    assert users_revision == "20261022_01", "Usuarios staging no está en la revisión de autenticación esperada"
    pg_versions = {
        db: _psql(f"{db}", db, "SHOW server_version")
        for db in ("usuarios_db", "eventos_db", "compras_db")
    }
    assert all(value.startswith("15.") for value in pg_versions.values()), "PostgreSQL staging no es versión 15 en todas las bases"
    old_events = _psql(
        "eventos_db", "eventos_db",
        "SELECT count(*) FROM eventos WHERE nombre LIKE 'nexustix-staging-recovery-%'",
    )
    old_claims = _psql(
        "compras_db", "compras_db",
        "SELECT count(*) FROM purchase_idempotency WHERE auth_subject LIKE 'nexustix-staging-recovery-test|%'",
    )
    assert old_events == old_claims == "0", "hay fixtures recovery sintéticas previas; no se crearán escenarios encima"
    stale_seconds = _safe_run([
        "docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE),
        "exec", "-T", "compras1", "python", "-c",
        "import os; print(max(30, int(os.getenv('PURCHASE_RECOVERY_STALE_SECONDS', '120'))))",
    ])
    for port in (18081, 18082, 18083, 18084, 18085):
        url = f"http://localhost:{port}/health"
        response = requests.get(url, timeout=8)
        assert response.status_code == 200, f"health HTTP {response.status_code} en puerto staging {port}"
        assert response.json().get("db") == "connected", f"DB no conectada en puerto staging {port}"
    return {"compras_alembic_revision": purchases_revision,
            "usuarios_alembic_revision": users_revision,
            "postgres_versions": ",".join(f"{db}={version}" for db, version in pg_versions.items()),
            "recovery_stale_seconds": stale_seconds,
            "old_synthetic_fixture_counts": "0/0"}


def _create_event(event_id: int, name: str) -> None:
    assert name.startswith("nexustix-staging-recovery-")
    assert event_id >= 1_000_000_000
    escaped_name = name.replace("'", "''")
    existing = _psql(
        "eventos_db", "eventos_db",
        f"SELECT count(*) FROM eventos WHERE id={event_id} OR nombre='{escaped_name}'",
    )
    assert existing == "0", "colisión con datos existentes; no se insertó este evento sintético"
    _psql(
        "eventos_db", "eventos_db",
        f"INSERT INTO eventos (id,nombre,stock) VALUES ({event_id},'{escaped_name}',2)",
    )


def _cleanup(spec: dict, result: dict) -> dict[str, int]:
    scenarios = spec["scenarios"]
    ids = [row["event_id"] for row in scenarios]
    ops = [row["operacion_id"] for row in scenarios]
    keys = [row["key"] for row in scenarios]
    purchases = sorted({purchase_id for values in result["purchase_ids"].values() for purchase_id in values})
    journals = result["recovery_journal_ids"]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert len(ops) == len(set(ops)) == 3 and len(keys) == len(set(keys)) == 3
    assert len(journals) == len(set(journals)) == 2
    assert len(purchases) == 2

    id_list = ",".join(str(int(value)) for value in ids)
    op_list = ",".join("'" + value + "'" for value in ops)
    key_list = ",".join("'" + value + "'" for value in keys)
    purchase_list = ",".join("'" + str(int(value)) + "'" for value in purchases)
    journal_list = ",".join(str(int(value)) for value in journals)
    exact_event_predicate = " OR ".join(
        f"(id={int(row['event_id'])} AND nombre='{row['event_name']}')" for row in scenarios
    )

    # Read-only guards on both databases run before either cleanup transaction.
    event_guard = _psql("eventos_db", "eventos_db", f"""
      SELECT (SELECT count(*) FROM eventos WHERE id IN ({id_list})) || ':' ||
             (SELECT count(*) FROM reservas_stock WHERE operacion_id IN ({op_list})) || ':' ||
             (SELECT count(*) FROM reservas_stock WHERE evento_id IN ({id_list}) AND operacion_id NOT IN ({op_list})) || ':' ||
             (SELECT string_agg(id::text || '=' || stock::text, ',' ORDER BY id) FROM eventos WHERE id IN ({id_list})) || ':' ||
             (SELECT count(*) FROM eventos WHERE id IN ({id_list}) AND nombre LIKE 'nexustix-staging-recovery-%')
    """)
    assert event_guard.split(":")[:3] == ["3", "3", "0"], "dependencias de Eventos no son exactamente las del test"
    event_stock = event_guard.split(":")[3]
    # Actual fixture ordering: confirmed case 1, no-reservation case 1 after retry,
    # released-reservation case 2. Derive by scenario order rather than random IDs.
    stock_by_id = {
        row["event_id"]: (1 if row["name"] in {
            "confirmed_purchase_pending_replay", "old_claim_without_reservation"
        } else 2)
        for row in scenarios
    }
    expected_stock = ",".join(f"{event_id}={stock_by_id[event_id]}" for event_id in sorted(ids))
    assert event_stock == expected_stock, "stock de eventos sintéticos no coincide con el resultado validado"
    names_guard = _psql("eventos_db", "eventos_db",
                        f"SELECT count(*) FROM eventos WHERE {exact_event_predicate}")
    assert names_guard == "3", "no coinciden exactamente las parejas ID/nombre sintéticas"

    purchase_guard = _psql("compras_db", "compras_db", f"""
      SELECT (SELECT count(*) FROM purchase_idempotency WHERE idempotency_key IN ({key_list})) || ':' ||
             (SELECT count(*) FROM compras WHERE operacion_id IN ({op_list})) || ':' ||
             (SELECT count(*) FROM journal_compras WHERE id IN ({journal_list}) AND tipo='RECOVERY') || ':' ||
             (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id' IN ({purchase_list})) || ':' ||
             (SELECT count(*) FROM journal_compras WHERE tipo='CONFIRMACION_PENDIENTE' AND detalles->>'operacion_id'='{ops[0]}')
    """)
    assert purchase_guard == "3:2:2:2:1", "las filas de Compras/journal no coinciden exactamente con el manifiesto"

    # Each DB cleanup uses a transaction and exact keys/IDs, with guards repeated
    # inside the transaction to catch a state change after the preflight reads.
    event_cleanup_sql = f"""
    BEGIN;
    DO $$ BEGIN
      IF (SELECT count(*) FROM eventos WHERE {exact_event_predicate}) <> 3 THEN RAISE EXCEPTION 'synthetic event guard'; END IF;
      IF (SELECT count(*) FROM reservas_stock WHERE operacion_id IN ({op_list})) <> 3 THEN RAISE EXCEPTION 'synthetic reservation guard'; END IF;
      IF EXISTS (SELECT 1 FROM reservas_stock WHERE evento_id IN ({id_list}) AND operacion_id NOT IN ({op_list})) THEN RAISE EXCEPTION 'unexpected reservation dependency'; END IF;
    END $$;
    DELETE FROM reservas_stock WHERE operacion_id IN ({op_list});
    DELETE FROM eventos WHERE {exact_event_predicate};
    COMMIT;
    SELECT (SELECT count(*) FROM eventos WHERE id IN ({id_list})) || ':' || (SELECT count(*) FROM reservas_stock WHERE operacion_id IN ({op_list}));
    """
    assert _psql("eventos_db", "eventos_db", event_cleanup_sql) == "0:0", "limpieza exacta de Eventos incompleta"

    purchase_cleanup_sql = f"""
    BEGIN;
    DO $$ BEGIN
      IF (SELECT count(*) FROM purchase_idempotency WHERE idempotency_key IN ({key_list})) <> 3 THEN RAISE EXCEPTION 'synthetic claim guard'; END IF;
      IF (SELECT count(*) FROM compras WHERE operacion_id IN ({op_list})) <> 2 THEN RAISE EXCEPTION 'synthetic purchase guard'; END IF;
      IF (SELECT count(*) FROM journal_compras WHERE id IN ({journal_list}) AND tipo='RECOVERY') <> 2 THEN RAISE EXCEPTION 'recovery journal guard'; END IF;
      IF (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id' IN ({purchase_list})) <> 2 THEN RAISE EXCEPTION 'commit journal guard'; END IF;
      IF (SELECT count(*) FROM journal_compras WHERE tipo='CONFIRMACION_PENDIENTE' AND detalles->>'operacion_id'='{ops[0]}') <> 1 THEN RAISE EXCEPTION 'pending journal guard'; END IF;
    END $$;
    DELETE FROM journal_compras WHERE id IN ({journal_list});
    DELETE FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id' IN ({purchase_list});
    DELETE FROM journal_compras WHERE tipo='CONFIRMACION_PENDIENTE' AND detalles->>'operacion_id'='{ops[0]}';
    DELETE FROM compras WHERE operacion_id IN ({op_list});
    DELETE FROM purchase_idempotency WHERE idempotency_key IN ({key_list});
    COMMIT;
    SELECT (SELECT count(*) FROM compras WHERE operacion_id IN ({op_list})) || ':' || (SELECT count(*) FROM purchase_idempotency WHERE idempotency_key IN ({key_list}));
    """
    assert _psql("compras_db", "compras_db", purchase_cleanup_sql) == "0:0", "limpieza exacta de Compras incompleta"
    return {"event_rows": 3, "reservation_rows": 3, "purchase_rows": 2,
            "claim_rows": 3, "journal_rows": 5}


def test_recovery_reconciles_real_postgres_and_events_without_duplicate_effects(request):
    preflight = _preflight()
    admin_session, admin_token, token_ttl = _password_and_admin_token()
    request.addfinalizer(lambda: _logout(admin_session))
    assert int(preflight["recovery_stale_seconds"]) + 60 < token_ttl, (
        "el umbral de recovery no cabe en el access token manual; no se escribieron fixtures"
    )
    run_id = uuid.uuid4().hex
    scenarios = []
    for index, name in enumerate((
        "confirmed_purchase_pending_replay",
        "old_claim_without_reservation",
        "old_reservation_without_purchase",
    )):
        scenarios.append({
            "name": name,
            "event_id": 1_000_000_000 + secrets.randbelow(900_000_000),
            "event_name": f"nexustix-staging-recovery-{run_id}-{index}",
            "key": f"recovery-{run_id}-{index}",
            "operacion_id": uuid.uuid4().hex,
            "synthetic_user_id": 1_900_000_000 + index,
        })
    spec = {"scenarios": scenarios}

    try:
        # The only direct INSERT is for these three collision-checked synthetic
        # Events rows. If a partial insert fails, retain and report exact fixtures.
        inserted = []
        for scenario in scenarios:
            _create_event(scenario["event_id"], scenario["event_name"])
            inserted.append(scenario)
    except Exception as exc:
        pytest.fail(f"falló la preparación de evento sintético; filas creadas={len(inserted)}; no se hizo limpieza amplia ({type(exc).__name__})")

    runner_path = ROOT / "tests/staging/purchase_recovery_pg_runner.py"
    runner_b64 = base64.b64encode(runner_path.read_bytes()).decode("ascii")
    runner_code = f"exec(compile(__import__('base64').b64decode('{runner_b64}'), '<staging-recovery-runner>', 'exec'))"
    runner_command = [
        "docker", "compose", "--project-name", PROJECT, "-f", str(COMPOSE),
        "exec", "-T", "compras1", "python", "-c", runner_code,
    ]
    # The access token is sent only via stdin. It is not present in argv or output.
    spec["admin_access_token"] = admin_token
    try:
        runner = subprocess.run(
            runner_command, cwd=ROOT, input=json.dumps(spec), capture_output=True,
            text=True, timeout=900, env=os.environ.copy(),
        )
    finally:
        del admin_token
        spec.pop("admin_access_token", None)

    try:
        result = json.loads(runner.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        pytest.fail(f"helper staging terminó sin manifiesto (código {runner.returncode}); se conservan los datos sintéticos")
    assert runner.returncode == 0 and result.get("status") == "verified", (
        f"prueba de recovery no verificada; etapa={result.get('stage')}; "
        f"causa={result.get('error_type')}; detalle={result.get('error_hint')}; "
        f"fixtures={result.get('scenario_manifest')}; "
        "se conservan los datos sintéticos para diagnóstico"
    )
    assert result.get("cleanup_safe") is True
    for scenario in spec["scenarios"]:
        scenario["operacion_id"] = result["operation_ids"][scenario["name"]]
    cleanup_counts = _cleanup(spec, result)
    result.pop("recovery_journal_ids", None)
    result.pop("operation_ids", None)
    logout = admin_session.post(
        f"{USERS_URL}/auth/logout",
        headers={"Origin": "http://localhost:18080", "X-CSRF-Protection": "1"},
        timeout=10,
    )
    assert logout.status_code == 200, f"logout QA HTTP {logout.status_code}"
    print(json.dumps({"preflight": preflight, "recovery": result,
                      "cleanup": cleanup_counts,
                      "admin_refresh_session_revoked": True,
                      "primary_environment_touched": False}, sort_keys=True))

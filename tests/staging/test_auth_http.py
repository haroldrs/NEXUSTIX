"""HTTP acceptance tests for the disposable Compose staging project only."""

import getpass
import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import urlparse

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ORIGIN = "http://localhost:18080"
USERS_URL = os.getenv("NEXUSTIX_STAGING_USERS_URL", "http://localhost:18081")
EVENTS_URL = os.getenv("NEXUSTIX_STAGING_EVENTS_URL", "http://localhost:18082")
PURCHASES_URL = os.getenv("NEXUSTIX_STAGING_PURCHASES_URL", "http://localhost:18085")
PURCHASE_REPLICA_URLS = (
    os.getenv("NEXUSTIX_STAGING_PURCHASES1_URL", "http://localhost:18083"),
    os.getenv("NEXUSTIX_STAGING_PURCHASES2_URL", "http://localhost:18084"),
)
pytestmark = pytest.mark.skipif(
    os.getenv("NEXUSTIX_STAGING_HTTP") != "1",
    reason="Requiere el staging NEXUSTIX desechable ya iniciado y migrado",
)


def new_buyer(email=None):
    session = requests.Session()
    email = email or f"qa-{uuid.uuid4().hex}@example.com"
    password = secrets.token_urlsafe(24)
    response = session.post(
        f"{USERS_URL}/auth/register",
        json={"nombre": "Comprador QA", "email": email, "password": password},
        timeout=8,
    )
    assert response.status_code == 201, f"registro HTTP {response.status_code}"
    payload = response.json()
    assert payload["user"]["role"] == "comprador"
    assert payload["user"]["email"] == email
    assert payload["expires_in"] > 0
    cookie = response.headers.get("Set-Cookie", "").lower()
    assert "nexustix_refresh=" in cookie and "httponly" in cookie and "path=/auth" in cookie
    assert "samesite=strict" in cookie
    assert "max-age=86400" in cookie
    return session, payload, email, password


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def csrf_headers():
    return {"Origin": FRONTEND_ORIGIN, "X-CSRF-Protection": "1"}


def staging_psql(service, database, sql):
    """Run a narrowly scoped SQL statement inside the named staging DB container."""
    if not os.getenv("NEXUSTIX_STAGING_SECRETS_DIR"):
        pytest.fail("Define NEXUSTIX_STAGING_SECRETS_DIR para usar exclusivamente el Compose aislado")
    command = [
        "docker", "compose", "--project-name", "nexustix-auth-staging",
        "-f", str(ROOT / "docker-compose.staging.yml"), "exec", "-T", service,
        "psql", "-U", "postgres", "-d", database, "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, f"consulta SQL staging falló en {service} (código {result.returncode})"
    return result.stdout.strip()


def _synthetic_event_snapshot(event_id, idempotency_key):
    event_raw = staging_psql(
        "eventos_db", "eventos_db",
        f"SELECT json_build_object('evento', json_build_object('id', e.id, 'nombre', e.nombre, 'stock', e.stock), 'reservas', coalesce(json_agg(json_build_object('operacion_id', r.operacion_id, 'cantidad', r.cantidad, 'estado', r.estado)) FILTER (WHERE r.operacion_id IS NOT NULL), '[]'::json)) FROM eventos e LEFT JOIN reservas_stock r ON r.evento_id=e.id WHERE e.id={event_id} GROUP BY e.id",
    )
    event = json.loads(event_raw) if event_raw else None
    purchases = json.loads(staging_psql(
        "compras_db", "compras_db",
        f"SELECT json_build_object('compras', coalesce((SELECT json_agg(json_build_object('id', c.id, 'usuario_id', c.usuario_id, 'auth_subject', c.auth_subject, 'evento_id', c.evento_id, 'cantidad', c.cantidad, 'estado', c.estado, 'operacion_id', c.operacion_id)) FROM compras c WHERE c.evento_id={event_id}), '[]'::json), 'claims', coalesce((SELECT json_agg(json_build_object('operacion_id', i.operacion_id, 'auth_subject', i.auth_subject, 'state', i.state, 'response_status', i.response_status, 'response_body', i.response_body)) FROM purchase_idempotency i WHERE i.idempotency_key='{idempotency_key}'), '[]'::json), 'journal_commit', (SELECT count(*) FROM journal_compras j WHERE j.tipo='COMMIT' AND j.detalles->>'compra_id' IN (SELECT id::text FROM compras WHERE evento_id={event_id})))",
    ))
    return {"eventos": event, "compras": purchases}


def _staging_service_token():
    secret_dir = Path(os.environ["NEXUSTIX_STAGING_SECRETS_DIR"]).expanduser()
    token_path = secret_dir / "eventos-service-token"
    assert token_path.is_file() and os.access(token_path, os.R_OK), "credencial interna de staging no disponible"
    mode = token_path.stat().st_mode & 0o777
    assert mode & 0o077 == 0, "permisos demasiado abiertos en credencial interna de staging"
    return token_path.read_text(encoding="utf-8").strip()


def _safe_synthetic_cleanup(event_id, event_name, idempotency_key, operation_id, purchase_id, email, user_id, expected_subject):
    """Delete only this test's rows after exact, read-only state checks."""
    # SQL values here are generated UUIDs, numeric IDs, or values returned by this
    # test's API. Refuse cleanup unless every expected linkage is unique.
    email_sql = "'" + email.replace("'", "''") + "'"
    name_sql = "'" + event_name.replace("'", "''") + "'"
    subject_sql = "'" + expected_subject.replace("'", "''") + "'"
    pre_cleanup = _synthetic_event_snapshot(event_id, idempotency_key)
    e = pre_cleanup["eventos"]["evento"]
    reservations = pre_cleanup["eventos"]["reservas"]
    buys = pre_cleanup["compras"]["compras"]
    claims = pre_cleanup["compras"]["claims"]
    assert e == {"id": event_id, "nombre": event_name, "stock": 2}, "estado del evento no coincide; se conserva para inspección"
    assert len(reservations) == 1 and reservations[0] == {
        "operacion_id": operation_id, "cantidad": 1, "estado": "confirmada",
    }, "reserva inesperada; se conserva para inspección"
    assert len(buys) == 1 and buys[0] == {
        "id": purchase_id, "usuario_id": user_id, "auth_subject": expected_subject,
        "evento_id": event_id, "cantidad": 1, "estado": "confirmada",
        "operacion_id": operation_id,
    }, "compra inesperada; se conserva para inspección"
    assert len(claims) == 1 and claims[0]["operacion_id"] == operation_id
    assert claims[0]["auth_subject"] == expected_subject
    assert claims[0]["state"] == "completed" and claims[0]["response_status"] == 200
    assert claims[0]["response_body"]["id"] == purchase_id
    assert claims[0]["response_body"]["confirmacion_reserva"] == "confirmada"
    assert pre_cleanup["compras"]["journal_commit"] == 1

    user_state = json.loads(staging_psql(
        "usuarios_db", "usuarios_db",
        f"SELECT json_build_object('users', count(DISTINCT u.id), 'sessions', count(s.id), 'active_sessions', count(s.id) FILTER (WHERE s.revoked_at IS NULL), 'email', min(u.email), 'user_id', min(u.id)) FROM usuarios u LEFT JOIN refresh_sessions s ON s.user_id=u.id WHERE u.email={email_sql} GROUP BY u.email",
    ))
    assert user_state == {"users": 1, "sessions": 1, "active_sessions": 0, "email": email, "user_id": user_id}, "sesiones o usuario inesperados; se conserva para inspección"
    assert staging_psql(
        "compras_db", "compras_db",
        f"SELECT count(*) FROM compras WHERE (auth_subject={subject_sql} OR usuario_id={user_id}) AND NOT (id={purchase_id} AND evento_id={event_id} AND operacion_id='{operation_id}')",
    ) == "0", "hay compras adicionales asociadas al usuario; se conserva para inspección"

    # Each DO block is a transaction with predicates for all links. A mismatch
    # raises and rolls back the block rather than broadening the deletion.
    staging_psql("compras_db", "compras_db", f"""
DO $$ BEGIN
  IF (SELECT count(*) FROM compras WHERE id={purchase_id} AND evento_id={event_id} AND operacion_id='{operation_id}' AND estado='confirmada' AND cantidad=1 AND auth_subject={subject_sql}) <> 1 THEN RAISE EXCEPTION 'synthetic purchase guard failed'; END IF;
  IF (SELECT count(*) FROM purchase_idempotency WHERE idempotency_key='{idempotency_key}' AND operacion_id='{operation_id}' AND auth_subject={subject_sql} AND state='completed') <> 1 THEN RAISE EXCEPTION 'synthetic claim guard failed'; END IF;
  IF (SELECT count(*) FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id'='{purchase_id}') <> 1 THEN RAISE EXCEPTION 'synthetic journal guard failed'; END IF;
  DELETE FROM journal_compras WHERE tipo='COMMIT' AND detalles->>'compra_id'='{purchase_id}';
  DELETE FROM compras WHERE id={purchase_id} AND evento_id={event_id} AND operacion_id='{operation_id}';
  DELETE FROM purchase_idempotency WHERE idempotency_key='{idempotency_key}' AND operacion_id='{operation_id}' AND auth_subject={subject_sql};
END $$;
""")
    staging_psql("eventos_db", "eventos_db", f"""
DO $$ BEGIN
  IF (SELECT count(*) FROM reservas_stock WHERE operacion_id='{operation_id}' AND evento_id={event_id} AND cantidad=1 AND estado='confirmada') <> 1 THEN RAISE EXCEPTION 'synthetic reservation guard failed'; END IF;
  IF (SELECT count(*) FROM eventos WHERE id={event_id} AND nombre={name_sql} AND stock=2) <> 1 THEN RAISE EXCEPTION 'synthetic event guard failed'; END IF;
  DELETE FROM reservas_stock WHERE operacion_id='{operation_id}' AND evento_id={event_id};
  DELETE FROM eventos WHERE id={event_id} AND nombre={name_sql} AND stock=2;
END $$;
""")
    staging_psql("usuarios_db", "usuarios_db", f"""
DO $$ BEGIN
  IF (SELECT count(*) FROM usuarios WHERE id={user_id} AND email={email_sql} AND role='comprador' AND auth_status='active') <> 1 THEN RAISE EXCEPTION 'synthetic user guard failed'; END IF;
  IF EXISTS (SELECT 1 FROM refresh_sessions WHERE user_id={user_id} AND revoked_at IS NULL) THEN RAISE EXCEPTION 'synthetic session still active'; END IF;
  DELETE FROM refresh_sessions WHERE user_id={user_id};
  DELETE FROM usuarios WHERE id={user_id} AND email={email_sql};
END $$;
""")

    after = _synthetic_event_snapshot(event_id, idempotency_key)
    assert after["eventos"] is None
    assert after["compras"] == {"compras": [], "claims": [], "journal_commit": 0}
    assert staging_psql("usuarios_db", "usuarios_db", f"SELECT count(*) FROM usuarios WHERE id={user_id} AND email={email_sql}") == "0"
    assert staging_psql("usuarios_db", "usuarios_db", f"SELECT count(*) FROM refresh_sessions WHERE user_id={user_id}") == "0"
    return pre_cleanup, after


def test_registration_login_and_authentication_required():
    session, registration, email, password = new_buyer()
    assert requests.get(f"{USERS_URL}/auth/me", timeout=8).status_code == 401
    assert requests.get(f"{PURCHASES_URL}/compras/mias", timeout=8).status_code == 401
    assert requests.get(f"{PURCHASES_URL}/compras", timeout=8).status_code == 401
    me = requests.get(f"{USERS_URL}/auth/me", headers=auth_headers(registration["access_token"]), timeout=8)
    assert me.status_code == 200 and me.json()["id"] == registration["user"]["id"]
    login = requests.post(f"{USERS_URL}/auth/login", json={"email": email, "password": password}, timeout=8)
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "comprador"
    escalation = requests.post(
        f"{USERS_URL}/auth/register",
        json={"nombre": "Escalation QA", "email": f"qa-{uuid.uuid4().hex}@example.com", "password": password, "role": "administrador"},
        timeout=8,
    )
    assert escalation.status_code == 422
    assert session.cookies.get("nexustix_refresh")


def test_buyer_purchase_is_owned_and_history_is_private():
    buyer, identity, _, _ = new_buyer()
    another_buyer, other_identity, _, _ = new_buyer()
    forged = buyer.post(
        f"{PURCHASES_URL}/compras",
        headers={**auth_headers(identity["access_token"]), "Idempotency-Key": f"forged-{uuid.uuid4().hex}"},
        json={"usuario_id": other_identity["user"]["id"], "evento_id": 1, "cantidad": 1},
        timeout=12,
    )
    assert forged.status_code == 422
    no_token = requests.post(
        f"{PURCHASES_URL}/compras",
        headers={"Idempotency-Key": f"anonymous-{uuid.uuid4().hex}"},
        json={"evento_id": 1, "cantidad": 1}, timeout=8,
    )
    assert no_token.status_code == 401
    purchase = buyer.post(
        f"{PURCHASES_URL}/compras",
        headers={**auth_headers(identity["access_token"]), "Idempotency-Key": f"purchase-{uuid.uuid4().hex}"},
        json={"evento_id": 1, "cantidad": 1},
        timeout=12,
    )
    assert purchase.status_code == 200, f"compra HTTP {purchase.status_code}"
    purchase_id = purchase.json()["id"]
    mine = buyer.get(f"{PURCHASES_URL}/compras/mias", headers=auth_headers(identity["access_token"]), timeout=8)
    theirs = another_buyer.get(f"{PURCHASES_URL}/compras/mias", headers=auth_headers(other_identity["access_token"]), timeout=8)
    assert mine.status_code == 200 and any(row["id"] == purchase_id for row in mine.json())
    assert theirs.status_code == 200 and all(row["id"] != purchase_id for row in theirs.json())
    hidden = another_buyer.get(f"{PURCHASES_URL}/compras/{purchase_id}", headers=auth_headers(other_identity["access_token"]), timeout=8)
    assert hidden.status_code == 404
    assert buyer.get(f"{PURCHASES_URL}/compras", headers=auth_headers(identity["access_token"]), timeout=8).status_code == 403
    assert buyer.get(f"{PURCHASES_URL}/auditoria/consistencia", headers=auth_headers(identity["access_token"]), timeout=8).status_code == 403
    assert buyer.get(f"{PURCHASES_URL}/compras/journal", headers=auth_headers(identity["access_token"]), timeout=8).status_code == 403


def test_purchase_idempotency_replays_and_rejects_changed_request():
    buyer, identity, _, _ = new_buyer()
    other, other_identity, _, _ = new_buyer()
    key = f"idem-{uuid.uuid4().hex}"
    headers = {**auth_headers(identity["access_token"]), "Idempotency-Key": key}
    payload = {"evento_id": 1, "cantidad": 1}
    first = buyer.post(f"{PURCHASES_URL}/compras", headers=headers, json=payload, timeout=15)
    replay = buyer.post(f"{PURCHASES_URL}/compras", headers=headers, json=payload, timeout=15)
    assert first.status_code == replay.status_code == 200
    assert first.json()["id"] == replay.json()["id"]
    assert replay.headers.get("Idempotency-Replayed") == "true"

    changed = buyer.post(
        f"{PURCHASES_URL}/compras", headers=headers,
        json={"evento_id": 2, "cantidad": 1}, timeout=8,
    )
    reused_by_other = other.post(
        f"{PURCHASES_URL}/compras",
        headers={**auth_headers(other_identity["access_token"]), "Idempotency-Key": key},
        json=payload, timeout=8,
    )
    assert changed.status_code == reused_by_other.status_code == 409


def test_concurrent_purchase_retries_create_one_order():
    buyer, identity, _, _ = new_buyer()
    key = f"idem-race-{uuid.uuid4().hex}"
    headers = {**auth_headers(identity["access_token"]), "Idempotency-Key": key}
    payload = {"evento_id": 1, "cantidad": 1}
    barrier = Barrier(2)

    def send():
        barrier.wait(timeout=5)
        return requests.post(f"{PURCHASES_URL}/compras", headers=headers, json=payload, timeout=20)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: send(), range(2)))
    assert all(response.status_code == 200 for response in results)
    assert len({response.json()["id"] for response in results}) == 1
    assert len({response.json()["operacion_id"] for response in results}) == 1


def test_shared_postgres_idempotency_across_both_purchase_replicas():
    """Real HTTP proof: same global key reaches each API process directly."""
    buyer, identity, _, _ = new_buyer()
    key = f"idem-cross-replica-{uuid.uuid4().hex}"
    headers = {**auth_headers(identity["access_token"]), "Idempotency-Key": key}
    payload = {"evento_id": 1, "cantidad": 1}
    first = requests.post(f"{PURCHASE_REPLICA_URLS[0]}/compras", headers=headers, json=payload, timeout=20)
    assert first.status_code == 200, f"Compras1 HTTP {first.status_code}"
    replay = requests.post(f"{PURCHASE_REPLICA_URLS[1]}/compras", headers=headers, json=payload, timeout=20)
    assert replay.status_code == 200, f"Compras2 HTTP {replay.status_code}"
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["operacion_id"] == first.json()["operacion_id"]

    # Race the same key through both independent workers. A unique PostgreSQL
    # constraint plus row lock must yield one purchase and one replay.
    race_key = f"idem-cross-race-{uuid.uuid4().hex}"
    race_headers = {**auth_headers(identity["access_token"]), "Idempotency-Key": race_key}
    barrier = Barrier(2)

    def send(replica_url):
        barrier.wait(timeout=5)
        return requests.post(f"{replica_url}/compras", headers=race_headers, json=payload, timeout=25)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, PURCHASE_REPLICA_URLS))
    assert all(response.status_code == 200 for response in results), [r.status_code for r in results]
    assert len({response.json()["id"] for response in results}) == 1
    assert len({response.json()["operacion_id"] for response in results}) == 1
    assert sum(response.headers.get("Idempotency-Replayed") == "true" for response in results) == 1


def test_oversell_attempt_is_rejected_without_changing_stock():
    buyer, identity, _, _ = new_buyer()
    event = requests.get(f"{EVENTS_URL}/eventos/1/stock", timeout=8)
    assert event.status_code == 200
    stock_before = event.json()["stock"]
    response = buyer.post(
        f"{PURCHASES_URL}/compras",
        headers={**auth_headers(identity["access_token"]), "Idempotency-Key": f"oversell-{uuid.uuid4().hex}"},
        json={"evento_id": 1, "cantidad": stock_before + 1},
        timeout=15,
    )
    assert response.status_code == 409
    event_after = requests.get(f"{EVENTS_URL}/eventos/1/stock", timeout=8)
    assert event_after.status_code == 200 and event_after.json()["stock"] == stock_before


def test_concurrent_different_buyers_cannot_oversell_synthetic_staging_event():
    """Real HTTP + shared PostgreSQL: two identities race for one synthetic ticket."""
    if not os.getenv("NEXUSTIX_STAGING_SECRETS_DIR"):
        pytest.fail("Define NEXUSTIX_STAGING_SECRETS_DIR; no se usan servicios ni bases principales")
    expected_urls = {
        USERS_URL: 18081,
        EVENTS_URL: 18082,
        PURCHASES_URL: 18085,
    }
    assert all(
        urlparse(url).hostname in {"localhost", "127.0.0.1"}
        and urlparse(url).port == expected_port
        and urlparse(url).scheme == "http"
        for url, expected_port in expected_urls.items()
    ), "la prueba de escritura solo admite los puertos locales definidos para staging"

    suffix = uuid.uuid4().hex
    event_id = 2_000_000_000 + (uuid.uuid4().int % 100_000_000)
    emails = [f"qa-oversell-{suffix}-a@example.com", f"qa-oversell-{suffix}-b@example.com"]
    keys = [f"qa-oversell-{suffix}-a", f"qa-oversell-{suffix}-b"]
    created_event = False
    try:
        inserted = staging_psql(
            "eventos_db", "eventos_db",
            f"INSERT INTO eventos (id, nombre, stock) VALUES ({event_id}, 'QA temporal {suffix}', 1) ON CONFLICT (id) DO NOTHING RETURNING id",
        )
        created_event = str(event_id) in inserted.splitlines()
        assert created_event, "el identificador sintético ya existía; no se ejecutó la carrera"

        event_before = json.loads(staging_psql(
            "eventos_db", "eventos_db",
            f"SELECT json_build_object('stock', e.stock, 'reservas', count(r.operacion_id), 'confirmadas', count(r.operacion_id) FILTER (WHERE r.estado='confirmada'), 'cantidad_confirmada', coalesce(sum(r.cantidad) FILTER (WHERE r.estado='confirmada'),0)) FROM eventos e LEFT JOIN reservas_stock r ON r.evento_id=e.id WHERE e.id={event_id} GROUP BY e.id",
        ))
        purchases_before = json.loads(staging_psql(
            "compras_db", "compras_db",
            f"SELECT json_build_object('compras', count(*), 'cantidad', coalesce(sum(cantidad),0), 'confirmadas', count(*) FILTER (WHERE estado='confirmada'), 'operaciones', count(DISTINCT operacion_id), 'journal_commit', (SELECT count(*) FROM journal_compras j WHERE j.tipo='COMMIT' AND j.detalles->>'compra_id' IN (SELECT id::text FROM compras WHERE evento_id={event_id}))) FROM compras WHERE evento_id={event_id}",
        ))
        assert event_before == {"stock": 1, "reservas": 0, "confirmadas": 0, "cantidad_confirmada": 0}
        assert purchases_before == {"compras": 0, "cantidad": 0, "confirmadas": 0, "operaciones": 0, "journal_commit": 0}

        buyers = [new_buyer(email) for email in emails]
        barrier = Barrier(2)

        def buy(index):
            session, identity, _, _ = buyers[index]
            barrier.wait(timeout=5)
            return session.post(
                f"{PURCHASES_URL}/compras",
                headers={**auth_headers(identity["access_token"]), "Idempotency-Key": keys[index]},
                json={"evento_id": event_id, "cantidad": 1}, timeout=25,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(buy, range(2)))
        assert sorted(response.status_code for response in responses) == [200, 409], [r.status_code for r in responses]
        winners = [response.json() for response in responses if response.status_code == 200]
        assert len(winners) == 1 and winners[0].get("confirmacion_reserva") == "confirmada"

        event_state = json.loads(staging_psql(
            "eventos_db", "eventos_db",
            f"SELECT json_build_object('stock', e.stock, 'reservas', count(r.operacion_id), 'confirmadas', count(r.operacion_id) FILTER (WHERE r.estado='confirmada'), 'cantidad_confirmada', coalesce(sum(r.cantidad) FILTER (WHERE r.estado='confirmada'),0)) FROM eventos e LEFT JOIN reservas_stock r ON r.evento_id=e.id WHERE e.id={event_id} GROUP BY e.id",
        ))
        purchase_state = json.loads(staging_psql(
            "compras_db", "compras_db",
            f"SELECT json_build_object('compras', count(*), 'cantidad', coalesce(sum(cantidad),0), 'confirmadas', count(*) FILTER (WHERE estado='confirmada'), 'operaciones', count(DISTINCT operacion_id), 'journal_commit', (SELECT count(*) FROM journal_compras j WHERE j.tipo='COMMIT' AND j.detalles->>'compra_id' IN (SELECT id::text FROM compras WHERE evento_id={event_id}))) FROM compras WHERE evento_id={event_id}",
        ))
        assert event_state == {"stock": 0, "reservas": 1, "confirmadas": 1, "cantidad_confirmada": 1}
        assert purchase_state == {"compras": 1, "cantidad": 1, "confirmadas": 1, "operaciones": 1, "journal_commit": 1}
        print(f"staging_synthetic_event={event_id} before={event_before}/{purchases_before} after={event_state}/{purchase_state}")
    finally:
        if created_event:
            # Elimina únicamente las filas vinculadas al evento, claves y cuentas de esta ejecución.
            keys_sql = ",".join("'" + key + "'" for key in keys)
            staging_psql("compras_db", "compras_db", f"DELETE FROM journal_compras WHERE detalles->>'compra_id' IN (SELECT id::text FROM compras WHERE evento_id={event_id}); DELETE FROM compras WHERE evento_id={event_id}; DELETE FROM purchase_idempotency WHERE idempotency_key IN ({keys_sql})")
            staging_psql("eventos_db", "eventos_db", f"DELETE FROM reservas_stock WHERE evento_id={event_id}; DELETE FROM eventos WHERE id={event_id} AND nombre='QA temporal {suffix}'")
            emails_sql = ",".join("'" + email + "'" for email in emails)
            staging_psql("usuarios_db", "usuarios_db", f"DELETE FROM refresh_sessions WHERE user_id IN (SELECT id FROM usuarios WHERE email IN ({emails_sql})); DELETE FROM usuarios WHERE email IN ({emails_sql})")


def test_http_purchase_idempotency_with_synthetic_event():
    """Full auth -> HTTP purchase -> replay proof; only fixture creation uses SQL."""
    assert os.getenv("NEXUSTIX_STAGING_SECRETS_DIR"), "Define NEXUSTIX_STAGING_SECRETS_DIR para limitar la prueba a staging"
    expected_urls = {
        USERS_URL: 18081,
        EVENTS_URL: 18082,
        PURCHASES_URL: 18085,
    }
    assert all(
        urlparse(url).scheme == "http"
        and urlparse(url).hostname in {"localhost", "127.0.0.1"}
        and urlparse(url).port == port
        for url, port in expected_urls.items()
    ), "la prueba de escritura solo admite URLs locales del staging autorizado"
    assert PURCHASE_REPLICA_URLS == ("http://localhost:18083", "http://localhost:18084")

    # Assert this exact Compose project, expected bindings, migration, and healthy
    # dependencies before any synthetic write. No resolved config is printed.
    env = os.environ.copy()
    env["NEXUSTIX_STAGING_SECRETS_DIR"] = str(Path(env["NEXUSTIX_STAGING_SECRETS_DIR"]).expanduser())
    config = subprocess.run(
        ["docker", "compose", "--project-name", "nexustix-auth-staging", "-f", str(ROOT / "docker-compose.staging.yml"), "config", "--quiet"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
    )
    assert config.returncode == 0, "Compose staging no validó (salida omitida para proteger configuración)"
    ps = subprocess.run(
        ["docker", "compose", "--project-name", "nexustix-auth-staging", "-f", str(ROOT / "docker-compose.staging.yml"), "ps", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
    )
    assert ps.returncode == 0, "no se pudo comprobar el proyecto Compose de staging"
    containers = [json.loads(line) for line in ps.stdout.splitlines() if line.strip()]
    required_services = {"usuarios", "eventos", "compras1", "compras2", "compras_lb", "usuarios_db", "eventos_db", "compras_db"}
    assert containers and all(row.get("Project") == "nexustix-auth-staging" for row in containers), "hay contenedores fuera del proyecto Compose permitido"
    assert required_services.issubset({row.get("Service") for row in containers})
    assert all(row.get("State") == "running" for row in containers if row.get("Service") in required_services)
    published = {
        row.get("Service"): {item.get("PublishedPort") for item in row.get("Publishers", []) if item.get("PublishedPort")}
        for row in containers
    }
    assert published.get("usuarios") == {18081}
    assert published.get("eventos") == {18082}
    assert published.get("compras1") == {18083}
    assert published.get("compras2") == {18084}
    assert published.get("compras_lb") == {18085}
    revisions = {
        "usuarios": staging_psql("usuarios_db", "usuarios_db", "SELECT version_num FROM alembic_version"),
        "compras": staging_psql("compras_db", "compras_db", "SELECT version_num FROM alembic_version"),
    }
    assert revisions == {"usuarios": "20261022_01", "compras": "20261023_02"}, "revisión de staging inesperada; no se creó ningún dato sintético"

    for url in (USERS_URL, EVENTS_URL, *PURCHASE_REPLICA_URLS, PURCHASES_URL):
        health = requests.get(f"{url}/health", timeout=8)
        assert health.status_code == 200, f"health HTTP no disponible en puerto {urlparse(url).port}"
        assert health.json().get("db") == "connected", f"base no conectada en puerto {urlparse(url).port}"

    suffix = uuid.uuid4().hex
    event_id = 2_000_000_000 + (uuid.uuid4().int % 100_000_000)
    event_name = f"NEXUSTIX QA HTTP {suffix}"
    email = f"qa-http-purchase-{suffix}@example.com"
    key = f"qa-http-purchase-{suffix}"
    operation_id = None
    purchase_id = None
    user_id = None
    created_event = False
    buyer = None
    identity = None
    try:
        # Refuse collisions across all relevant staging tables before the insert.
        assert staging_psql("eventos_db", "eventos_db", f"SELECT count(*) FROM eventos WHERE id={event_id} OR nombre='{event_name}'") == "0"
        assert staging_psql("eventos_db", "eventos_db", f"SELECT count(*) FROM reservas_stock WHERE evento_id={event_id}") == "0"
        assert staging_psql("compras_db", "compras_db", f"SELECT count(*) FROM compras WHERE evento_id={event_id}") == "0"
        assert staging_psql("compras_db", "compras_db", f"SELECT count(*) FROM purchase_idempotency WHERE idempotency_key='{key}'") == "0"
        assert staging_psql("usuarios_db", "usuarios_db", f"SELECT count(*) FROM usuarios WHERE email='{email}'") == "0"

        inserted = staging_psql(
            "eventos_db", "eventos_db",
            f"INSERT INTO eventos (id, nombre, stock) VALUES ({event_id}, '{event_name}', 3) RETURNING id",
        )
        assert inserted == str(event_id), "no se confirmó la inserción única del evento sintético"
        created_event = True
        initial = _synthetic_event_snapshot(event_id, key)
        assert initial == {
            "eventos": {"evento": {"id": event_id, "nombre": event_name, "stock": 3}, "reservas": []},
            "compras": {"compras": [], "claims": [], "journal_commit": 0},
        }
        print(f"synthetic_event_created id={event_id} initial_stock=3; fixture prepared directly in staging PostgreSQL")

        buyer, identity, registered_email, password = new_buyer(email)
        assert registered_email == email
        del password
        user_id = identity["user"]["id"]
        assert identity["user"]["role"] == "comprador"
        assert buyer.get(f"{USERS_URL}/auth/me", headers=auth_headers(identity["access_token"]), timeout=8).json()["id"] == user_id
        token_parts = identity["access_token"].split(".")
        assert len(token_parts) == 3
        token_payload = token_parts[1] + "=" * (-len(token_parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(token_payload.encode("ascii")))
        assert str(claims.get("sub")) == str(user_id) and claims.get("role") == "comprador"
        subject = f"{claims['iss']}|{claims['sub']}"

        event_http = requests.get(f"{EVENTS_URL}/eventos/{event_id}/stock", timeout=8)
        assert event_http.status_code == 200 and event_http.json()["stock"] == 3
        request_body = {"evento_id": event_id, "cantidad": 1}
        headers = {**auth_headers(identity["access_token"]), "Idempotency-Key": key}
        first = buyer.post(f"{PURCHASES_URL}/compras", headers=headers, json=request_body, timeout=20)
        assert first.status_code == 200, f"compra HTTP {first.status_code}"
        first_body = first.json()
        purchase_id = first_body.get("id")
        operation_id = first_body.get("operacion_id")
        assert purchase_id and operation_id
        assert first_body.get("evento_id") == event_id and first_body.get("cantidad") == 1
        assert first_body.get("estado") == "confirmada" and first_body.get("confirmacion_reserva") == "confirmada"

        reservation_http = requests.get(
            f"{EVENTS_URL}/eventos/reservas/{operation_id}",
            headers={"X-Service-Token": _staging_service_token()}, timeout=8,
        )
        assert reservation_http.status_code == 200
        reservation_body = reservation_http.json()
        assert reservation_body == {
            "operacion_id": operation_id, "evento_id": event_id,
            "cantidad": 1, "estado": "confirmada", "stock_actual": 2,
        }

        replay = buyer.post(f"{PURCHASES_URL}/compras", headers=headers, json=request_body, timeout=20)
        assert replay.status_code == 200 and replay.headers.get("Idempotency-Replayed") == "true"
        assert replay.json() == first_body
        assert replay.json().get("id") == purchase_id and replay.json().get("operacion_id") == operation_id
        changed = buyer.post(
            f"{PURCHASES_URL}/compras", headers=headers,
            json={"evento_id": event_id, "cantidad": 2}, timeout=12,
        )
        assert changed.status_code == 409, f"misma clave con cuerpo distinto: HTTP {changed.status_code}"

        stock_http = requests.get(f"{EVENTS_URL}/eventos/{event_id}/stock", timeout=8)
        assert stock_http.status_code == 200 and stock_http.json()["stock"] == 2
        final = _synthetic_event_snapshot(event_id, key)
        assert final["eventos"] == {
            "evento": {"id": event_id, "nombre": event_name, "stock": 2},
            "reservas": [{"operacion_id": operation_id, "cantidad": 1, "estado": "confirmada"}],
        }
        claim = final["compras"]["claims"]
        assert final["compras"]["compras"] == [{
            "id": purchase_id, "usuario_id": user_id, "auth_subject": subject,
            "evento_id": event_id, "cantidad": 1, "estado": "confirmada",
            "operacion_id": operation_id,
        }]
        assert len(claim) == 1 and claim[0]["operacion_id"] == operation_id
        assert claim[0]["auth_subject"] == subject
        assert claim[0]["state"] == "completed" and claim[0]["response_status"] == 200
        assert claim[0]["response_body"]["confirmacion_reserva"] == "confirmada"
        assert final["compras"]["journal_commit"] == 1

        logout = buyer.post(f"{USERS_URL}/auth/logout", headers=csrf_headers(), timeout=8)
        assert logout.status_code == 200, f"logout HTTP {logout.status_code}"
        pre_cleanup, cleaned = _safe_synthetic_cleanup(
            event_id, event_name, key, operation_id, purchase_id,
            email, user_id, subject,
        )
        created_event = False
        print(
            f"http_purchase_status={first.status_code} replay_status={replay.status_code} "
            f"replayed={replay.headers.get('Idempotency-Replayed')} changed_body_status={changed.status_code} "
            f"event_stock={initial['eventos']['evento']['stock']}->{final['eventos']['evento']['stock']} "
            f"purchase/reservation/claim=1/1/1; cleanup=verified; user_id={user_id}; event_id={event_id}"
        )
    except Exception:
        # Keep any unexpected partial state for inspection; the error output
        # identifies the synthetic event, while no token/password is emitted.
        if created_event:
            print(f"synthetic_state_retained_for_review event_id={event_id}")
        raise


def test_administrator_can_read_global_sales():
    email = os.getenv("NEXUSTIX_STAGING_ADMIN_EMAIL")
    if not email:
        pytest.skip("Crea el administrador de staging tras la autorización y define NEXUSTIX_STAGING_ADMIN_EMAIL")
    password_file = os.getenv("NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE")
    password = (
        Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
        if password_file
        else getpass.getpass("Contraseña del administrador de staging (no se muestra): ")
    )
    response = requests.post(f"{USERS_URL}/auth/login", json={"email": email, "password": password}, timeout=8)
    assert response.status_code == 200
    token = response.json()["access_token"]
    assert response.json()["user"]["role"] == "administrador"
    sales = requests.get(f"{PURCHASES_URL}/compras", headers=auth_headers(token), timeout=8)
    assert sales.status_code == 200 and isinstance(sales.json(), list)
    audit = requests.get(f"{PURCHASES_URL}/auditoria/consistencia", headers=auth_headers(token), timeout=12)
    assert audit.status_code == 200, f"auditoría HTTP {audit.status_code}"
    journal = requests.get(f"{PURCHASES_URL}/compras/journal", headers=auth_headers(token), timeout=8)
    assert journal.status_code == 200 and isinstance(journal.json().get("entradas"), list)


def test_internal_events_operations_require_service_credential():
    secret_dir = Path(os.environ["NEXUSTIX_STAGING_SECRETS_DIR"])
    service_token = (secret_dir / "eventos-service-token").read_text(encoding="utf-8").strip()
    endpoint = f"{EVENTS_URL}/eventos/reservas/pendientes"
    assert requests.get(endpoint, timeout=8).status_code == 401
    assert requests.get(endpoint, headers={"X-Service-Token": "invalid-staging-token"}, timeout=8).status_code == 401
    accepted = requests.get(endpoint, headers={"X-Service-Token": service_token}, timeout=8)
    assert accepted.status_code == 200 and isinstance(accepted.json().get("reservas"), list)

    operation_id = uuid.uuid4().hex
    exact_status = f"{EVENTS_URL}/eventos/reservas/{operation_id}"
    assert requests.get(exact_status, timeout=8).status_code == 401
    reserve = requests.post(
        f"{EVENTS_URL}/eventos/1/reservar",
        params={"cantidad": 1, "operacion_id": operation_id},
        headers={"X-Service-Token": service_token},
        timeout=8,
    )
    assert reserve.status_code == 200
    verified = requests.get(exact_status, headers={"X-Service-Token": service_token}, timeout=8)
    assert verified.status_code == 200
    assert verified.json()["operacion_id"] == operation_id
    assert verified.json()["estado"] == "reservada"
    release = requests.post(
        f"{EVENTS_URL}/eventos/1/liberar",
        params={"cantidad": 1, "operacion_id": operation_id},
        headers={"X-Service-Token": service_token},
        timeout=8,
    )
    assert release.status_code == 200


def test_refresh_rotation_reuse_revokes_family():
    session, identity, _, _ = new_buyer()
    original = session.cookies.get("nexustix_refresh")
    rotated = session.post(f"{USERS_URL}/auth/refresh", headers=csrf_headers(), timeout=8)
    assert rotated.status_code == 200
    replacement = session.cookies.get("nexustix_refresh")
    assert replacement and replacement != original
    replay = requests.post(
        f"{USERS_URL}/auth/refresh",
        headers=csrf_headers(),
        cookies={"nexustix_refresh": original},
        timeout=8,
    )
    assert replay.status_code == 401
    revoked = session.post(f"{USERS_URL}/auth/refresh", headers=csrf_headers(), timeout=8)
    assert revoked.status_code == 401
    # Logout/replay revokes refresh credentials, while a bearer access token remains valid until exp.
    still_valid = requests.get(f"{USERS_URL}/auth/me", headers=auth_headers(identity["access_token"]), timeout=8)
    assert still_valid.status_code == 200


def test_logout_clears_refresh_cookie_but_does_not_revoke_existing_access_token():
    session, identity, _, _ = new_buyer()
    logout = session.post(f"{USERS_URL}/auth/logout", headers=csrf_headers(), timeout=8)
    assert logout.status_code == 200
    set_cookie = logout.headers.get("Set-Cookie", "").lower()
    assert "nexustix_refresh=" in set_cookie and "max-age=0" in set_cookie
    assert not session.cookies.get("nexustix_refresh")
    assert session.post(f"{USERS_URL}/auth/refresh", headers=csrf_headers(), timeout=8).status_code == 401
    # JWT access tokens are stateless and remain valid until their configured expiry.
    still_valid = requests.get(f"{USERS_URL}/auth/me", headers=auth_headers(identity["access_token"]), timeout=8)
    assert still_valid.status_code == 200


def test_concurrent_refresh_reuse_allows_at_most_one_rotation():
    session, _, _, _ = new_buyer()
    original = session.cookies.get("nexustix_refresh")
    barrier = Barrier(2)

    def rotate_once():
        barrier.wait(timeout=5)
        return requests.post(
            f"{USERS_URL}/auth/refresh",
            headers=csrf_headers(),
            cookies={"nexustix_refresh": original},
            timeout=12,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: rotate_once(), range(2)))
    statuses = sorted(response.status_code for response in results)
    assert statuses == [200, 401]
    successful = next(response for response in results if response.status_code == 200)
    replacement = successful.cookies.get("nexustix_refresh")
    assert replacement
    # The losing replay should revoke the entire family, including the winning replacement.
    final = requests.post(
        f"{USERS_URL}/auth/refresh",
        headers=csrf_headers(),
        cookies={"nexustix_refresh": replacement},
        timeout=8,
    )
    assert final.status_code == 401


def test_expired_access_token_refreshes_to_a_new_valid_access_token():
    session, identity, _, _ = new_buyer()
    time.sleep(identity["expires_in"] + 1)
    expired = requests.get(f"{USERS_URL}/auth/me", headers=auth_headers(identity["access_token"]), timeout=8)
    assert expired.status_code == 401
    refreshed = session.post(f"{USERS_URL}/auth/refresh", headers=csrf_headers(), timeout=8)
    assert refreshed.status_code == 200
    new_access = refreshed.json()["access_token"]
    current = requests.get(f"{USERS_URL}/auth/me", headers=auth_headers(new_access), timeout=8)
    assert current.status_code == 200


def run_frontend_http_renewal(identity, refresh_cookie, mode):
    config = {
        "USUARIOS_URL": USERS_URL,
        "EVENTOS_URL": EVENTS_URL,
        "COMPRAS_LB_URL": PURCHASES_URL,
        "COMPRAS_R1_URL": os.getenv("NEXUSTIX_STAGING_PURCHASES_R1_URL", "http://localhost:18083"),
        "COMPRAS_R2_URL": os.getenv("NEXUSTIX_STAGING_PURCHASES_R2_URL", "http://localhost:18084"),
    }
    payload = {
        "config": config,
        "access_token": identity["access_token"],
        "user": identity["user"],
        "refresh_cookie": refresh_cookie,
        "mode": mode,
    }
    result = subprocess.run(
        ["node", str(ROOT / "tests/staging/frontend_http_renewal.js")],
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "NEXUSTIX_STAGING_FRONTEND_ORIGIN": FRONTEND_ORIGIN},
    )
    assert result.returncode == 0, f"integración frontend HTTP falló: {result.stderr.strip()}"
    return result.stdout.strip()


def test_frontend_automatically_renews_concurrent_protected_requests():
    session, identity, _, _ = new_buyer()
    refresh_cookie = session.cookies.get("nexustix_refresh")
    time.sleep(identity["expires_in"] + 1)
    output = run_frontend_http_renewal(identity, refresh_cookie, "renew")
    assert "single refresh" in output


def test_frontend_restores_session_from_refresh_cookie():
    session, identity, _, _ = new_buyer()
    refresh_cookie = session.cookies.get("nexustix_refresh")
    output = run_frontend_http_renewal(identity, refresh_cookie, "restore")
    assert "restored identity" in output


def test_frontend_prompts_login_when_refresh_session_is_revoked():
    session, identity, _, _ = new_buyer()
    refresh_cookie = session.cookies.get("nexustix_refresh")
    logout = session.post(f"{USERS_URL}/auth/logout", headers=csrf_headers(), timeout=8)
    assert logout.status_code == 200
    time.sleep(identity["expires_in"] + 1)
    output = run_frontend_http_renewal(identity, refresh_cookie, "revoked")
    assert "prompted login" in output


def test_expired_refresh_token_is_rejected_by_http_endpoint():
    session, _, _, _ = new_buyer()
    raw_token = session.cookies.get("nexustix_refresh")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    sql = f"UPDATE refresh_sessions SET expires_at = now() - interval '1 second' WHERE token_hash = '{token_hash}'"
    compose = [
        "docker", "compose", "-p", "nexustix-auth-staging", "-f", str(ROOT / "docker-compose.staging.yml"),
        "exec", "-T", "-u", "postgres", "usuarios_db", "psql", "-X", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", "usuarios_db", "-c", sql,
    ]
    subprocess.run(compose, check=True, capture_output=True, text=True, timeout=12)
    expired = session.post(f"{USERS_URL}/auth/refresh", headers=csrf_headers(), timeout=8)
    assert expired.status_code == 401


def test_frontend_keeps_access_token_in_memory_and_refresh_is_http_only():
    app_js = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
    users_main = (ROOT / "usuarios/main.py").read_text(encoding="utf-8")
    assert "let ACCESS_TOKEN = null" in app_js
    assert "localStorage" not in app_js and "sessionStorage" not in app_js and "document.cookie" not in app_js
    assert "httponly=True" in users_main
    assert "path=\"/auth\"" in users_main
    submit_purchase = app_js[app_js.index("async function submitPurchase"):app_js.index("async function loadAdminEvents")]
    assert 'document.getElementById("auth-open").addEventListener' not in submit_purchase
    assert 'document.getElementById("auth-open").addEventListener("click"' in app_js
    assert 'restoreSession();' in app_js[app_js.index('document.addEventListener("DOMContentLoaded"'):]
    assert app_js.count('if (CURRENT_USER?.role !== "administrador")') >= 2
    assert "let REFRESH_PROMISE = null" in app_js
    assert 'navigator.locks.request("nexustix-auth-refresh"' in app_js
    assert "return fetchJSON(url, options, timeoutMs, true)" in app_js
    assert "if (refreshed.status === 401)" in app_js
    compose = (ROOT / "docker-compose.staging.yml").read_text(encoding="utf-8")
    test_compose = (ROOT / "docker-compose.staging.tests.yml").read_text(encoding="utf-8")
    assert "NEXUSTIX_STAGING_ACCESS_TOKEN_TTL_SECONDS:-600" in compose
    assert 'NEXUSTIX_STAGING_TEST_ACCESS_TOKEN_TTL_SECONDS:-15' in test_compose
    assert 'REFRESH_TOKEN_TTL_DAYS: "1"' in compose


def test_frontend_clears_purchase_notice_and_routes_by_backend_role():
    app_js = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
    users_main = (ROOT / "usuarios/main.py").read_text(encoding="utf-8")
    assert 'document.getElementById("purchase-result")' in app_js
    assert "function clearPurchaseStatus()" in app_js
    assert "delete result.dataset.outcome" in app_js
    assert "clearPurchaseStatus();" in app_js[app_js.index("function clearAuthenticated"):app_js.index("async function restoreSession")]
    assert "previousUser?.id !== nextUser?.id" in app_js
    assert 'name === "admin" || name === "observabilidad"' in app_js
    assert 'CURRENT_USER?.role !== "administrador"' in app_js[app_js.index("function setView"):app_js.index("function showNotice")]
    assert 'setRegistrationAvailable(false)' in app_js
    assert 'requestLoginAfterExpiry();' in app_js
    assert 'role="comprador"' in users_main[users_main.index('def register'):users_main.index('def login')]
    assert '"role": user.role' in users_main or 'role=user.role' in users_main


def test_staging_frontend_serves_authentication_ui_and_staging_api_config():
    page = requests.get(FRONTEND_ORIGIN, timeout=8)
    config = requests.get(f"{FRONTEND_ORIGIN}/config.js", timeout=8)
    app = requests.get(f"{FRONTEND_ORIGIN}/app.js", timeout=8)
    assert page.status_code == 200 and 'id="auth-form"' in page.text
    assert 'data-auth-mode="register"' in page.text and 'data-auth-mode="login"' in page.text
    assert config.status_code == 200
    for port in (18081, 18082, 18083, 18084, 18085):
        assert f"localhost:{port}" in config.text
    assert app.status_code == 200
    assert 'document.getElementById("auth-open").addEventListener("click"' in app.text
    assert 'restoreSession();' in app.text


def test_staging_purchase_instances_and_load_balancer_are_healthy():
    for url in ("http://localhost:18083", "http://localhost:18084", PURCHASES_URL):
        response = requests.get(f"{url}/health", timeout=8)
        assert response.status_code == 200, f"Compras {url} HTTP {response.status_code}"
        assert response.json().get("status") == "ok"

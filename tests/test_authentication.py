"""Isolated auth and authorization contract tests (SQLite only; never use service DBs)."""
import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]


def import_service(service, tmp_path):
    for name in ("main", "models", "database"):
        sys.modules.pop(name, None)
    service_dir = ROOT / service
    sys.path.insert(0, str(service_dir))
    os.environ["DATABASE_URL"] = "sqlite://"
    os.environ["JWT_PUBLIC_KEY_FILE"] = str(tmp_path / "jwt_public.pem")
    os.environ["JWT_PRIVATE_KEY_FILE"] = str(tmp_path / "jwt_private.pem")
    os.environ["EVENTOS_SERVICE_TOKEN_FILE"] = str(tmp_path / "service_token")
    os.environ["AUTH_COOKIE_SECURE"] = "false"
    (tmp_path / "service_token").write_text("isolated-test-service-token\n")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "jwt_private.pem").write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (tmp_path / "jwt_public.pem").write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    database = importlib.import_module("database")
    importlib.import_module("models")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    models = importlib.import_module("models")
    if service == "compras":
        models.Compra.__table__.create(engine)
        models.RegistroJournal.__table__.create(engine)
        models.IdempotencyRequest.__table__.create(engine)
    else:
        database.Base.metadata.create_all(engine)
    app = importlib.import_module("main").app

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[database.get_db] = override_db
    return app, database, TestingSession, engine


@pytest.fixture
def users_app(tmp_path):
    app, database, sessions, engine = import_service("usuarios", tmp_path)
    yield app, database, sessions
    app.dependency_overrides.clear()
    engine.dispose()


def test_public_registration_is_buyer_only_and_password_is_argon2(users_app):
    app, _, sessions = users_app
    main = importlib.import_module("main")
    payload = main.RegisterRequest.model_validate({"nombre": "Ana", "email": " ANA@example.com ", "password": "una-clave-segura-123"})
    with sessions() as db:
        response = Response()
        result = main.register(payload, response, db)
        user = db.query(importlib.import_module("models").Usuario).one()
        assert user.password_hash.startswith("$argon2id$")
        assert user.role == "comprador"
        assert result["user"]["role"] == "comprador"
        assert response.headers.get("set-cookie", "").startswith("nexustix_refresh=")
    with pytest.raises(ValidationError):
        main.RegisterRequest.model_validate({"nombre": "Mallory", "email": "m@example.com", "password": "una-clave-segura-123", "role": "administrador"})


def test_login_and_buyer_cannot_access_admin_user_list(users_app):
    _, _, sessions = users_app
    main = importlib.import_module("main")
    with sessions() as db:
        main.register(main.RegisterRequest(nombre="Ana", email="ana@example.com", password="una-clave-segura-123"), Response(), db)
        login = main.login(main.LoginRequest(email="ANA@example.com", password="una-clave-segura-123"), Response(), db)
        buyer = db.query(importlib.import_module("models").Usuario).one()
        with pytest.raises(HTTPException) as denied:
            main.require_admin(buyer)
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException) as invalid:
            main.login(main.LoginRequest(email="ana@example.com", password="incorrecta"), Response(), db)
        assert invalid.value.status_code == 401
        assert login["user"]["role"] == "comprador"


def test_refresh_rotates_and_reuse_revokes_the_family(users_app):
    _, _, sessions = users_app
    main = importlib.import_module("main")
    with sessions() as db:
        registration_response = Response()
        main.register(main.RegisterRequest(nombre="Ana", email="ana@example.com", password="una-clave-segura-123"), registration_response, db)
        raw_token = registration_response.headers["set-cookie"].split("nexustix_refresh=", 1)[1].split(";", 1)[0]
        request = Request({"type": "http", "method": "POST", "path": "/auth/refresh", "headers": [(b"origin", b"http://localhost:8080")], "query_string": b"", "server": ("test", 80), "client": ("test", 123), "scheme": "http"})
        request._cookies = {"nexustix_refresh": raw_token}
        rotated_response = Response()
        result = main.refresh_session(request, rotated_response, db, "1")
        rotated_raw = rotated_response.headers["set-cookie"].split("nexustix_refresh=", 1)[1].split(";", 1)[0]
        assert result["access_token"] and rotated_raw != raw_token
        request._cookies = {"nexustix_refresh": raw_token}
        with pytest.raises(HTTPException) as replay:
            main.refresh_session(request, Response(), db, "1")
        assert replay.value.status_code == 401


@pytest.fixture
def purchases_app(tmp_path):
    app, database, sessions, engine = import_service("compras", tmp_path)
    model = importlib.import_module("models").Compra
    with sessions() as db:
        db.add_all([
            model(usuario_id=1, evento_id=1, cantidad=2, estado="confirmada", auth_subject="nexustix-usuarios|1"),
            model(usuario_id=2, evento_id=2, cantidad=1, estado="confirmada", auth_subject="nexustix-usuarios|2"),
            model(usuario_id=1, evento_id=3, cantidad=1, estado="confirmada", auth_subject=None),
        ])
        db.commit()
    yield app, database, sessions, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()


def signed_token(tmp_path, subject, role="comprador"):
    from shared.auth_tokens import issue_access_token
    return issue_access_token(subject, role, (tmp_path / "jwt_private.pem").read_text())


def test_purchase_history_uses_signed_subject_and_legacy_rows_stay_unclaimed(purchases_app):
    app, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    token = signed_token(tmp_path, 1)
    principal = main.get_principal(f"Bearer {token}")
    with sessions() as db:
        assert [row["evento_id"] for row in main.listar_mis_compras(db, principal)] == [1]
        with pytest.raises(HTTPException) as denied:
            main.require_admin(principal)
        assert denied.value.status_code == 403
        owned = main.obtener_compra(1, db, principal)
        assert owned["evento_id"] == 1
        other = main.get_principal(f"Bearer {signed_token(tmp_path, 2)}")
        with pytest.raises(HTTPException) as forbidden:
            main.obtener_compra(1, db, other)
        assert forbidden.value.status_code == 404
        with pytest.raises(ValidationError):
            main.CompraRequest.model_validate({"usuario_id": 2, "evento_id": 1, "cantidad": 1})


def test_admin_can_list_global_purchases(purchases_app):
    app, _, _, tmp_path = purchases_app
    token = signed_token(tmp_path, 9, "administrador")
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    principal = main.get_principal(f"Bearer {token}")
    with sessions() as db:
        main.require_admin(principal)
        assert len(main.listar_compras(db, principal)) == 3


@pytest.mark.parametrize("service", ["usuarios", "eventos", "compras"])
def test_health_is_unavailable_without_exposing_database_errors(service, tmp_path):
    app, _, _, engine = import_service(service, tmp_path)
    try:
        main = importlib.import_module("main")

        class BrokenSession:
            rolled_back = False

            def execute(self, _statement):
                raise SQLAlchemyError("synthetic-secret-database-detail")

            def rollback(self):
                self.rolled_back = True

        db = BrokenSession()
        response = Response()
        result = main.health(response, db)
        assert response.status_code == 503
        assert result["status"] == "unavailable"
        assert result["db"] == "unavailable"
        assert "synthetic-secret-database-detail" not in repr(result)
        assert db.rolled_back
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class StubEventsResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected mocked HTTP status {self.status_code}")


def configure_purchase_stubs(monkeypatch, main, *, reserve_status=200):
    from urllib.parse import urlparse

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        params = kwargs.get("params", {})
        event_id = int(urlparse(url).path.split("/")[2])
        operation_id = params.get("operacion_id", "isolated-op")
        quantity = params.get("cantidad", 1)
        if url.endswith("/reservar"):
            payload = {"evento_id": event_id, "cantidad": quantity, "operacion_id": operation_id,
                       "estado_reserva": "reservada", "stock_restante": 9,
                       "idempotente": False, "reloj_logico_T": 1,
                       "exclusion_mutua": {"mecanismo": "test"}}
            return StubEventsResponse(reserve_status, payload)
        if url.endswith("/confirmar"):
            return StubEventsResponse(200, {"status": "ok", "evento_id": event_id,
                "cantidad": quantity, "operacion_id": operation_id,
                "estado_reserva": "confirmada", "idempotente": False, "reloj_logico_T": 2})
        return StubEventsResponse(200, {"evento_id": event_id, "cantidad": quantity,
            "operacion_id": operation_id, "estado_reserva": "liberada",
            "stock_restante": 10, "idempotente": False, "reloj_logico_T": 2})

    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    monkeypatch.setattr(main, "registrar_journal", lambda *args, **kwargs: {})
    return calls


def test_purchase_idempotency_replays_original_and_rejects_key_reuse(purchases_app, monkeypatch):
    import json

    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    calls = configure_purchase_stubs(monkeypatch, main)
    buyer = main.get_principal(f"Bearer {signed_token(tmp_path, 31)}")
    payload = main.CompraRequest(evento_id=1, cantidad=1)

    with sessions() as db:
        original = main.crear_compra(payload, "qa-idempotency-one", db, buyer)
    with sessions() as db:
        replay = main.crear_compra(payload, "qa-idempotency-one", db, buyer)

    replay_body = json.loads(replay.body)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay_body == original
    assert replay_body["confirmacion_reserva"] == "confirmada"
    assert len(calls) == 2  # Una reserva y una confirmación, sin segundo flujo.
    with sessions() as db:
        assert db.query(models.Compra).count() == 4  # Tres compras del fixture + una nueva.
        assert db.query(models.IdempotencyRequest).count() == 1
    other_user = main.get_principal(f"Bearer {signed_token(tmp_path, 32)}")
    with sessions() as db, pytest.raises(HTTPException) as conflict_user:
        main.crear_compra(payload, "qa-idempotency-one", db, other_user)
    assert conflict_user.value.status_code == 409

    with sessions() as db, pytest.raises(HTTPException) as conflict_body:
        main.crear_compra(main.CompraRequest(evento_id=2, cantidad=1), "qa-idempotency-one", db, buyer)
    assert conflict_body.value.status_code == 409


def test_group_purchase_uses_validated_reservation_and_confirmation(purchases_app, monkeypatch):
    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    calls = configure_purchase_stubs(monkeypatch, main)
    buyer = main.get_principal(f"Bearer {signed_token(tmp_path, 35)}")
    request = main.TransaccionGrupalRequest(items=[{"evento_id": 71, "cantidad": 1}])

    with sessions() as db:
        result = main.crear_compra_grupal(request, db, buyer)

    assert result["total_items"] == 1
    assert result["confirmacion_stock_completa"] is True
    assert result["confirmaciones_stock"][0]["confirmada"] is True
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == ["reservar", "confirmar"]
    with sessions() as db:
        purchases = db.query(models.Compra).filter_by(evento_id=71).all()
        assert len(purchases) == 1 and purchases[0].estado == "confirmada"


def test_unverifiable_events_reservation_response_keeps_claim_ambiguous(purchases_app, monkeypatch):
    """An invalid Events response must not be turned into a confirmed purchase."""
    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")

    class InvalidJsonResponse:
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("invalid Events JSON")

    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    monkeypatch.setattr(main, "registrar_journal", lambda *args, **kwargs: {})
    monkeypatch.setattr(main.requests, "post", lambda *_args, **_kwargs: InvalidJsonResponse())
    import requests
    monkeypatch.setattr(
        main.requests, "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("isolated status timeout")),
    )
    principal = main.get_principal(f"Bearer {signed_token(tmp_path, 41)}")
    key = "qa-events-invalid-response"
    with sessions() as db:
        response = main.crear_compra(main.CompraRequest(evento_id=91, cantidad=1), key, db, principal)
    assert response.status_code == 503
    assert b"recuperacion_requerida" in response.body
    with sessions() as db:
        claim = db.query(models.IdempotencyRequest).filter_by(idempotency_key=key).one()
        assert claim.state == "processing"
        assert claim.response_status is None and claim.response_body is None
        assert db.query(models.Compra).filter_by(operacion_id=claim.operacion_id).count() == 0
        operation_id = claim.operacion_id

    # The status lookup also failed, so the key stays resumable and no sale is
    # persisted. The same operation ID is retained for a later safe retry.
    assert operation_id


def test_events_reservation_contract_rejects_missing_types_and_mismatches(purchases_app):
    _, _, _, _ = purchases_app
    main = importlib.import_module("main")
    valid = {"evento_id": 7, "cantidad": 2, "operacion_id": "op-7",
             "estado_reserva": "reservada", "stock_restante": 5,
             "idempotente": False, "reloj_logico_T": 12}
    assert main._validate_reserve_payload(valid, "op-7", 7, 2) == valid

    invalid_payloads = [
        {},
        {**valid, "evento_id": True},
        {**valid, "cantidad": "2"},
        {**valid, "operacion_id": "other-op"},
        {**valid, "evento_id": 8},
        {**valid, "cantidad": 1},
        {**valid, "estado_reserva": "confirmada"},
        {**valid, "stock_restante": -1},
        {**valid, "idempotente": 0},
        {**valid, "reloj_logico_T": "12"},
    ]
    for payload in invalid_payloads:
        with pytest.raises(main.EventsResponseUnverifiable):
            main._validate_reserve_payload(payload, "op-7", 7, 2)

    with pytest.raises(main.EventsResponseUnverifiable):
        main._events_json_object(StubEventsResponse(200, []), "test")

    confirmation = {"status": "ok", "evento_id": 7, "cantidad": 2,
                    "operacion_id": "op-7", "estado_reserva": "confirmada",
                    "idempotente": True, "reloj_logico_T": 13}
    assert main._validate_action_payload(confirmation, "confirmar", "op-7", 7, 2)
    release = {"evento_id": 7, "cantidad": 2, "operacion_id": "op-7",
               "estado_reserva": "liberada", "stock_restante": 7,
               "idempotente": True, "reloj_logico_T": 14}
    assert main._validate_action_payload(release, "liberar", "op-7", 7, 2)
    for action, payload in (
        ("confirmar", {**confirmation, "estado_reserva": "liberada"}),
        ("confirmar", {**confirmation, "cantidad": 3}),
        ("confirmar", {**confirmation, "status": "ok", "idempotente": 1}),
        ("liberar", {**release, "operacion_id": "wrong-op"}),
        ("liberar", {**release, "stock_restante": "7"}),
    ):
        with pytest.raises(main.EventsResponseUnverifiable):
            main._validate_action_payload(payload, action, "op-7", 7, 2)


def test_timeout_after_durable_reservation_continues_with_verified_operation(purchases_app, monkeypatch):
    import requests

    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    state = {"reservation_committed": False, "operation_id": None, "reserve_calls": 0}

    def post(url, **kwargs):
        params = kwargs["params"]
        event_id = int(url.split("/eventos/")[1].split("/")[0])
        op_id = params["operacion_id"]
        quantity = params.get("cantidad", 1)
        if url.endswith("/reservar"):
            state["reserve_calls"] += 1
            state["operation_id"] = op_id
            state["reservation_committed"] = True
            raise requests.exceptions.ReadTimeout("response lost after durable commit")
        return StubEventsResponse(200, {"status": "ok", "evento_id": event_id,
            "cantidad": quantity, "operacion_id": op_id,
            "estado_reserva": "confirmada", "idempotente": False, "reloj_logico_T": 2})

    def get(url, **_kwargs):
        assert url.endswith(f"/reservas/{state['operation_id']}")
        return StubEventsResponse(200, {"operacion_id": state["operation_id"],
            "evento_id": 7, "cantidad": 1, "estado": "reservada", "stock_actual": 4})

    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    monkeypatch.setattr(main, "registrar_journal", lambda *args, **kwargs: {})
    principal = main.get_principal(f"Bearer {signed_token(tmp_path, 42)}")
    with sessions() as db:
        result = main.crear_compra(main.CompraRequest(evento_id=7, cantidad=1),
                                   "qa-events-ack-lost", db, principal)
    assert result["confirmacion_reserva"] == "confirmada"
    assert result["operacion_id"] == state["operation_id"]
    assert state["reserve_calls"] == 1
    with sessions() as db:
        assert db.query(models.Compra).filter_by(operacion_id=state["operation_id"]).count() == 1
        assert db.query(models.IdempotencyRequest).filter_by(
            idempotency_key="qa-events-ack-lost").one().state == "completed"


def test_invalid_reservation_response_and_unavailable_status_remain_503(purchases_app, monkeypatch):
    import json
    import requests

    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        params = kwargs["params"]
        return StubEventsResponse(200, {"evento_id": 8, "cantidad": 1,
            "operacion_id": "wrong-operation", "estado_reserva": "reservada",
            "stock_restante": 3, "idempotente": False, "reloj_logico_T": 1})

    def get(url, **_kwargs):
        if url.endswith("/reservas/pendientes"):
            return StubEventsResponse(200, {"total": 0, "reservas": []})
        raise requests.exceptions.ReadTimeout("cannot verify durable state")

    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    principal = main.get_principal(f"Bearer {signed_token(tmp_path, 43)}")
    key = "qa-events-response-mismatch"
    with sessions() as db:
        result = main.crear_compra(main.CompraRequest(evento_id=7, cantidad=1), key, db, principal)
    assert result.status_code == 503
    body = json.loads(result.body)
    assert body["detail"]["operacion_id"]
    assert calls == [f"{main.EVENTOS_URL}/eventos/7/reservar"]
    with sessions() as db:
        claim = db.query(models.IdempotencyRequest).filter_by(idempotency_key=key).one()
        assert claim.state == "processing"
        assert db.query(models.Compra).filter_by(operacion_id=claim.operacion_id).count() == 0


def test_invalid_action_ack_requires_exact_durable_status(purchases_app, monkeypatch):
    import requests

    _, _, _, _ = purchases_app
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    monkeypatch.setattr(main.requests, "post", lambda *_args, **_kwargs: StubEventsResponse(200, {
        "status": "ok", "evento_id": 7, "cantidad": 1,
        "operacion_id": "wrong-operation", "estado_reserva": "confirmada",
        "idempotente": False, "reloj_logico_T": 2,
    }))
    monkeypatch.setattr(main.requests, "get", lambda *_args, **_kwargs: StubEventsResponse(200, {
        "operacion_id": "operation-7", "evento_id": 7, "cantidad": 1,
        "estado": "confirmada", "stock_actual": 4,
    }))
    verified = main._perform_event_action("confirmar", "operation-7", 7, 1, 3)
    assert verified["estado_reserva"] == "confirmada"
    assert verified["operacion_id"] == "operation-7"
    assert verified["stock_restante"] == 4

    monkeypatch.setattr(
        main.requests, "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("isolated")),
    )
    with pytest.raises(main.EventsResponseUnverifiable):
        main._perform_event_action("liberar", "operation-7", 7, 1, 3)


def _seed_recovery_claim(sessions, models, *, key, op_id, state="processing", created_at=None, body=None, purchase=None):
    from datetime import datetime, timedelta, timezone

    with sessions() as db:
        record = models.IdempotencyRequest(
            idempotency_key=key,
            auth_subject="nexustix-usuarios|501",
            request_hash="a" * 64,
            operacion_id=op_id,
            state=state,
            response_status=200 if state == "completed" else None,
            response_body=body,
            created_at=created_at or datetime.now(timezone.utc) - timedelta(seconds=main_recovery_stale_seconds()),
        )
        db.add(record)
        if purchase:
            db.add(models.Compra(
                id=purchase["id"], usuario_id=501, evento_id=purchase["evento_id"],
                cantidad=purchase["cantidad"], estado="confirmada",
                operacion_id=op_id, auth_subject="nexustix-usuarios|501", instancia="test",
            ))
        db.commit()


def main_recovery_stale_seconds():
    return importlib.import_module("main").RECOVERY_STALE_SECONDS + 10


def prepare_recovery(monkeypatch, main):
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    monkeypatch.setattr(main, "registrar_journal", lambda *args, **kwargs: {})
    monkeypatch.setattr(main, "auditar_consistencia", lambda *_args, **_kwargs: {
        "todo_consistente": True, "auditoria_completa": True,
    })
    return main.Principal(user_id=1, role="administrador", subject="nexustix-usuarios|1")


def test_recovery_repairs_replay_after_events_confirmed_without_duplicate_purchase(purchases_app, monkeypatch):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "recovery-confirmed-op"
    _seed_recovery_claim(
        sessions, models, key="recovery-confirmed-key", op_id=op_id,
        state="completed", body={"id": 88, "operacion_id": op_id, "confirmacion_reserva": "pendiente"},
        purchase={"id": 88, "evento_id": 9, "cantidad": 2},
    )
    principal = prepare_recovery(monkeypatch, main)
    get_calls = []
    post_calls = []

    def get(url, **_kwargs):
        get_calls.append(url)
        if url.endswith("/reservas/pendientes"):
            return StubEventsResponse(200, {"total": 0, "reservas": []})
        return StubEventsResponse(200, {
            "operacion_id": op_id, "evento_id": 9, "cantidad": 2,
            "estado": "confirmada", "stock_actual": 8,
        })

    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: post_calls.append(args[0]))
    with sessions() as db:
        result = main.recuperar_servidor(db, principal)

    assert result["contadores"]["replays_actualizados"] == 1
    assert post_calls == []
    assert len([url for url in get_calls if url.endswith(f"/reservas/{op_id}")]) == 1
    with sessions() as db:
        assert db.query(models.Compra).filter_by(operacion_id=op_id).count() == 1
        stored = db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one()
        assert stored.state == "completed"
        assert stored.response_body["confirmacion_reserva"] == "confirmada"
        assert stored.response_body["id"] == 88
        assert stored.response_body["operacion_id"] == op_id


def test_recovery_diagnoses_stale_claim_without_reservation_without_mutation(purchases_app, monkeypatch):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "recovery-no-reservation"
    _seed_recovery_claim(sessions, models, key="recovery-no-reservation-key", op_id=op_id)
    principal = prepare_recovery(monkeypatch, main)
    posts = []

    def get(url, **_kwargs):
        if url.endswith("/reservas/pendientes"):
            return StubEventsResponse(200, {"total": 0, "reservas": []})
        return StubEventsResponse(404, {"detail": "not found"})

    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: posts.append(args[0]))
    with sessions() as db:
        result = main.recuperar_servidor(db, principal)

    assert posts == []
    assert any(item["tipo"] == "claim_sin_reserva_reintentable" for item in result["diagnosticos"])
    with sessions() as db:
        assert db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one().state == "processing"
        assert db.query(models.Compra).count() == 3


def test_recovery_skips_live_claim_held_by_another_request(purchases_app, monkeypatch):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "recovery-live-claim"
    _seed_recovery_claim(sessions, models, key="recovery-live-key", op_id=op_id)
    principal = prepare_recovery(monkeypatch, main)
    monkeypatch.setattr(main, "_try_lock_recovery_record", lambda *_args: (None, True))
    gets = []
    monkeypatch.setattr(main.requests, "get", lambda url, **_kwargs: (gets.append(url) or StubEventsResponse(200, {"total": 0, "reservas": []})))
    with sessions() as db:
        result = main.recuperar_servidor(db, principal)

    assert result["contadores"]["omitidas_en_proceso"] == 1
    assert not any(url.endswith(f"/reservas/{op_id}") for url in gets)
    with sessions() as db:
        assert db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one().state == "processing"


def test_recovery_lock_classifier_does_not_hide_other_database_errors(purchases_app):
    from types import SimpleNamespace
    from sqlalchemy.exc import OperationalError

    main = importlib.import_module("main")

    class LockUnavailable(Exception):
        pgcode = "55P03"

    class FakeQuery:
        def __init__(self, error):
            self.error = error

        def filter(self, *_args):
            return self

        def with_for_update(self, **_kwargs):
            return self

        def first(self):
            raise self.error

    class FakeSession:
        def __init__(self, error):
            self.error = error

        def query(self, *_args):
            return FakeQuery(self.error)

        def rollback(self):
            pass

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    locked = OperationalError("SELECT", {}, LockUnavailable("row lock is held"))
    assert main._try_lock_recovery_record(FakeSession(locked), "live-op") == (None, True)

    unavailable = OperationalError("SELECT", {}, RuntimeError("connection refused"))
    with pytest.raises(OperationalError):
        main._try_lock_recovery_record(FakeSession(unavailable), "db-down-op")


def test_recovery_keeps_ambiguous_events_status_untouched(purchases_app, monkeypatch):
    import requests

    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "recovery-ambiguous-status"
    _seed_recovery_claim(sessions, models, key="recovery-ambiguous-key", op_id=op_id)
    principal = prepare_recovery(monkeypatch, main)
    posts = []
    calls = {"pending": 0}

    def get(url, **_kwargs):
        if url.endswith("/reservas/pendientes"):
            return StubEventsResponse(200, {"total": 0, "reservas": []})
        raise requests.exceptions.ReadTimeout("isolated ambiguous status")

    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: posts.append(args[0]))
    with sessions() as db:
        result = main.recuperar_servidor(db, principal)

    assert posts == []
    assert any(item["tipo"] == "estado_eventos_no_verificable" for item in result["diagnosticos"])
    with sessions() as db:
        assert db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one().state == "processing"
        assert db.query(models.Compra).count() == 3


def test_recovery_of_stale_reserved_claim_is_idempotent_across_repeated_runs(purchases_app, monkeypatch):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "recovery-repeat-release"
    _seed_recovery_claim(sessions, models, key="recovery-repeat-key", op_id=op_id)
    principal = prepare_recovery(monkeypatch, main)
    state = {"reservation": "reservada", "release_calls": 0}

    def get(url, **_kwargs):
        if url.endswith("/reservas/pendientes"):
            pending = [{"operacion_id": op_id, "evento_id": 10, "cantidad": 1,
                        "estado": "reservada"}] if state["reservation"] == "reservada" else []
            return StubEventsResponse(200, {"total": len(pending), "reservas": pending})
        return StubEventsResponse(200, {
            "operacion_id": op_id, "evento_id": 10, "cantidad": 1,
            "estado": state["reservation"], "stock_actual": 3,
        })

    def post(url, **_kwargs):
        assert url.endswith("/liberar")
        state["release_calls"] += 1
        state["reservation"] = "liberada"
        return StubEventsResponse(200, {"evento_id": 10, "cantidad": 1,
            "operacion_id": op_id, "estado_reserva": "liberada",
            "stock_restante": 4, "idempotente": False, "reloj_logico_T": 1})

    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", post)
    with sessions() as db:
        first = main.recuperar_servidor(db, principal)
    with sessions() as db:
        second = main.recuperar_servidor(db, principal)

    assert first["contadores"]["reservas_liberadas"] == 1
    assert state["release_calls"] == 1
    assert second["contadores"]["reservas_liberadas"] == 0
    with sessions() as db:
        assert db.query(models.Compra).filter_by(operacion_id=op_id).count() == 0
        record = db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one()
        assert record.state == "completed" and record.response_status == 409

def test_purchase_idempotency_caches_reservation_error(purchases_app, monkeypatch):
    import json

    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    calls = configure_purchase_stubs(monkeypatch, main, reserve_status=409)
    buyer = main.get_principal(f"Bearer {signed_token(tmp_path, 41)}")
    payload = main.CompraRequest(evento_id=1, cantidad=1)
    with sessions() as db:
        first = main.crear_compra(payload, "qa-idempotency-error", db, buyer)
    with sessions() as db:
        replay = main.crear_compra(payload, "qa-idempotency-error", db, buyer)

    assert first.status_code == replay.status_code == 409
    assert json.loads(first.body) == json.loads(replay.body)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(calls) == 1
    with sessions() as db:
        assert db.query(models.Compra).count() == 3  # El rechazo no agrega compras.
        assert db.query(models.IdempotencyRequest).count() == 1


def test_ambiguous_events_timeout_keeps_claim_and_retry_reuses_operation_id(purchases_app, monkeypatch):
    import json
    import requests

    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    seen_operation_ids = []
    attempts = {"reserve": 0}

    def post(url, **kwargs):
        if url.endswith("/reservar"):
            operation_id = kwargs["params"]["operacion_id"]
            seen_operation_ids.append(operation_id)
            attempts["reserve"] += 1
            if attempts["reserve"] == 1:
                raise requests.exceptions.ReadTimeout("isolated ambiguous timeout")
            return StubEventsResponse(200, {"evento_id": 1, "cantidad": 1,
                "operacion_id": operation_id, "estado_reserva": "reservada",
                "stock_restante": 8, "idempotente": True, "reloj_logico_T": 1})
        params = kwargs.get("params", {})
        return StubEventsResponse(200, {"status": "ok", "evento_id": 1,
            "cantidad": 1, "operacion_id": params.get("operacion_id"),
            "estado_reserva": "confirmada", "idempotente": False, "reloj_logico_T": 2})

    def get(url, **_kwargs):
        if url.endswith("/reservas/pendientes"):
            return StubEventsResponse(200, {"total": 0, "reservas": []})
        raise requests.exceptions.ReadTimeout("isolated durable status unavailable")

    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    monkeypatch.setattr(main, "registrar_journal", lambda *args, **kwargs: {})
    buyer = main.get_principal(f"Bearer {signed_token(tmp_path, 52)}")
    payload = main.CompraRequest(evento_id=1, cantidad=1)

    with sessions() as db:
        ambiguous = main.crear_compra(payload, "qa-ambiguous-idem", db, buyer)
    assert ambiguous.status_code == 503
    with sessions() as db:
        claim = db.query(models.IdempotencyRequest).one()
        assert claim.state == "processing"
        durable_operation_id = claim.operacion_id

    with sessions() as db:
        completed = main.crear_compra(payload, "qa-ambiguous-idem", db, buyer)
    assert isinstance(completed, dict)
    assert seen_operation_ids == [durable_operation_id, durable_operation_id]
    assert json.loads(json.dumps(completed))["operacion_id"] == durable_operation_id
    with sessions() as db:
        assert db.query(models.Compra).count() == 4
        assert db.query(models.IdempotencyRequest).one().state == "completed"


def test_consistency_audit_reports_journal_gaps_without_claiming_journal_as_truth(purchases_app, monkeypatch):
    _, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    principal = main.get_principal(f"Bearer {signed_token(tmp_path, 77, 'administrador')}")

    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    responses = iter([
        FakeResponse([{"id": 1, "nombre": "A", "stock_inicial": 10, "stock_actual": 8, "operaciones_confirmadas": []}]),
        FakeResponse({"total": 0, "reservas": []}),
    ])
    monkeypatch.setattr(main.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    with sessions() as db:
        report = main.auditar_consistencia(db, principal)
    assert report["journal"]["es_fuente_de_verdad"] is False
    assert report["journal"]["completo"] is False
    assert report["journal"]["compras_confirmadas_sin_commit_journal"] == [1, 2, 3]


def test_purchase_idempotency_concurrent_requests_share_one_purchase(purchases_app, monkeypatch, tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    _, _, _, key_dir = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    sqlite_path = tmp_path / "parallel-idempotency.sqlite"
    parallel_engine = create_engine(
        f"sqlite:///{sqlite_path}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(parallel_engine, "connect")
    def set_busy_timeout(connection, _record):
        connection.execute("PRAGMA busy_timeout=15000")

    models.Compra.__table__.create(parallel_engine)
    models.RegistroJournal.__table__.create(parallel_engine)
    models.IdempotencyRequest.__table__.create(parallel_engine)
    ParallelSession = sessionmaker(bind=parallel_engine, autoflush=False, autocommit=False)
    calls = []
    calls_lock = threading.Lock()

    def post(url, **kwargs):
        with calls_lock:
            calls.append(url)
        params = kwargs.get("params", {})
        event_id = int(url.rstrip("/").split("/")[-2])
        operation_id = params.get("operacion_id")
        quantity = params.get("cantidad", 1)
        if url.endswith("/reservar"):
            time.sleep(0.15)
            return StubEventsResponse(200, {"evento_id": event_id, "cantidad": quantity,
                "operacion_id": operation_id, "estado_reserva": "reservada",
                "stock_restante": 8, "idempotente": False, "reloj_logico_T": 1})
        return StubEventsResponse(200, {"status": "ok", "evento_id": event_id,
            "cantidad": quantity, "operacion_id": operation_id,
            "estado_reserva": "confirmada", "idempotente": False, "reloj_logico_T": 2})

    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main, "eventos_headers", lambda: {"X-Service-Token": "isolated"})
    monkeypatch.setattr(main, "registrar_journal", lambda *args, **kwargs: {})
    buyer = main.get_principal(f"Bearer {signed_token(key_dir, 51)}")
    start = threading.Barrier(2)

    def send_request():
        with ParallelSession() as db:
            start.wait(timeout=5)
            return main.crear_compra(main.CompraRequest(evento_id=1, cantidad=1), "qa-concurrent-idem", db, buyer)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: send_request(), range(2)))
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(getattr(result, "headers", {}).get("Idempotency-Replayed") == "true" for result in results) == 1
        assert len(calls) == 2  # Solo una reserva y su confirmación.
        with ParallelSession() as db:
            assert db.query(models.Compra).count() == 1
            assert db.query(models.IdempotencyRequest).count() == 1
    finally:
        parallel_engine.dispose()


@pytest.fixture
def events_app(tmp_path):
    app, database, _, engine = import_service("eventos", tmp_path)
    yield app, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()


def test_internal_events_endpoint_requires_service_credential(events_app):
    _, _ = events_app
    main = importlib.import_module("main")
    with pytest.raises(HTTPException) as denied:
        main.require_service_token(None)
    assert denied.value.status_code == 401
    assert main.require_service_token("isolated-test-service-token") is None


def test_frontend_keeps_access_token_only_in_memory_and_refresh_is_http_only():
    app_js = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
    users_main = (ROOT / "usuarios/main.py").read_text(encoding="utf-8")
    assert "let ACCESS_TOKEN = null" in app_js
    assert "localStorage" not in app_js and "sessionStorage" not in app_js
    assert "document.cookie" not in app_js
    assert 'credentials: "include"' in app_js
    assert "httponly=True" in users_main and "secure=_cookie_secure()" in users_main


def test_purchase_frontends_send_idempotency_key_and_cors_allows_it():
    main_frontend = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
    # Demo y pruebas sirven el mismo app.js; solo cambia config.js.
    staging_frontend = main_frontend
    purchases_main = (ROOT / "compras/main.py").read_text(encoding="utf-8")
    for script in (main_frontend, staging_frontend):
        assert '"Idempotency-Key": key' in script
        assert "PENDING_PURCHASE_KEYS" in script
    assert '"Idempotency-Key"' in purchases_main[purchases_main.index("allow_headers="):purchases_main.index(")", purchases_main.index("allow_headers="))]


def test_compras_simulation_shutdown_health_and_failover_status(purchases_app):
    app, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    with sessions() as db:
        orig_apagado = main.SIMULACION_APAGADO
        try:
            main.SIMULACION_APAGADO = True
            response = Response()
            res = main.health(response, db)
            assert response.status_code == 503
            assert res["status"] == "down"
            assert "apagada abruptamente" in res["motivo"].lower()

            with pytest.raises(HTTPException) as exc_info:
                main.validar_nodo_disponible()
            assert exc_info.value.status_code == 503
            assert exc_info.value.detail["estado"] == "apagado_simulado"
        finally:
            main.SIMULACION_APAGADO = orig_apagado


def test_compras_simulation_routes_require_admin(purchases_app):
    app, _, sessions, tmp_path = purchases_app
    main = importlib.import_module("main")
    buyer = main.get_principal(f"Bearer {signed_token(tmp_path, 1, 'comprador')}")
    admin = main.get_principal(f"Bearer {signed_token(tmp_path, 9, 'administrador')}")

    with pytest.raises(HTTPException) as denied:
        main.require_admin(buyer)
    assert denied.value.status_code == 403
    assert main.require_admin(admin) == admin


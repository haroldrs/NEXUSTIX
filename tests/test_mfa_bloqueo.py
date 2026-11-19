"""MFA (TOTP) y bloqueo por intentos fallidos del servicio Usuarios (SQLite aislado)."""
import importlib
import json
import time

import pytest
from fastapi import HTTPException, Response

from tests.test_authentication import import_service


@pytest.fixture
def users(tmp_path, monkeypatch):
    app, database, sessions, engine = import_service("usuarios", tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    with sessions() as db:
        main.register(main.RegisterRequest.model_validate({"nombre": "Ana", "email": "ana@example.com", "password": "una-clave-segura-123"}), Response(), db)
        user_id = db.query(models.Usuario).one().id
    yield main, models, sessions, user_id
    app.dependency_overrides.clear()
    engine.dispose()


def login(main, sessions, password="una-clave-segura-123", codigo=None):
    payload = {"email": "ana@example.com", "password": password}
    if codigo is not None:
        payload["codigo"] = codigo
    with sessions() as db:
        try:
            result = main.login(main.LoginRequest.model_validate(payload), Response(), db)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}
    if isinstance(result, dict):
        return 200, result
    return result.status_code, json.loads(result.body)


def test_totp_matches_rfc6238_vector(users):
    totp = importlib.import_module("totp")
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    # RFC 6238, apéndice B (SHA1, T = 59 s): 94287082 -> últimos 6 dígitos.
    assert totp.code_at(secret, 59 // 30) == "287082"
    step = totp.verify(secret, "287082", now=59)
    assert step == 1
    assert totp.verify(secret, "287082", last_step=step, now=59) is None  # no se reutiliza


def test_five_failures_lock_the_account_and_success_resets(users, monkeypatch):
    main, models, sessions, user_id = users
    for _ in range(main.LOGIN_MAX_ATTEMPTS - 1):
        assert login(main, sessions, "clave-incorrecta-999")[0] == 401
    status, body = login(main, sessions, "clave-incorrecta-999")
    assert status == 429 and body["bloqueado"] and body["segundos"] > 0
    # Bloqueada: ni la contraseña correcta entra.
    assert login(main, sessions)[0] == 429
    with sessions() as db:
        user = db.get(models.Usuario, user_id)
        user.locked_until = None
        db.commit()
    assert login(main, sessions)[0] == 200
    with sessions() as db:
        assert db.get(models.Usuario, user_id).failed_logins == 0


def test_mfa_requires_code_after_password(users):
    main, models, sessions, user_id = users
    totp = importlib.import_module("totp")
    with sessions() as db:
        actor = db.get(models.Usuario, user_id)
        secret = main.mfa_setup(db, actor)["secreto"]
    with sessions() as db:
        actor = db.get(models.Usuario, user_id)
        assert main.mfa_enable(main.MfaCodeRequest(codigo=totp.code_at(secret, totp.current_step())), db, actor) == {"activo": True}
    status, body = login(main, sessions)
    assert status == 401 and body["mfa_requerido"] and "access_token" not in body
    assert login(main, sessions, codigo="000000")[0] == 401
    # El código usado para activar no se acepta otra vez; el del intervalo siguiente sí.
    next_code = totp.code_at(secret, totp.current_step() + 1)
    status, body = login(main, sessions, codigo=next_code)
    assert status == 200 and body["access_token"]
    assert login(main, sessions, codigo=next_code)[0] == 401


def test_frontend_shows_mfa_code_and_admin_card():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "frontend/index.html").read_text(encoding="utf-8")
    app = (root / "frontend/app.js").read_text(encoding="utf-8")
    assert 'id="auth-code"' in html and 'id="mfa-configure"' in html and 'id="unlock-form"' in html
    assert "mfa_requerido" in app and "payload.codigo" in app

"""Fixtures para pruebas contra un PostgreSQL real y DESECHABLE.

Opt-in: NEXUSTIX_TEST_PG_ADMIN_URL=postgresql://postgres:CLAVE@127.0.0.1:55432/postgres
(scripts/ejecutar_pruebas.py levanta un contenedor temporal con esa URL).

Salvaguardas: solo hosts loopback; se rechaza un servidor que contenga bases
usuarios_db/eventos_db/compras_db (sería uno del proyecto); cada prueba crea
su propia base nxtest_<aleatorio> y elimina solo esa base al terminar.
"""
import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.getenv("NEXUSTIX_TEST_PG_ADMIN_URL")
PROJECT_DATABASES = {"usuarios_db", "eventos_db", "compras_db"}


def _admin_engine():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(ADMIN_URL)
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.skip("NEXUSTIX_TEST_PG_ADMIN_URL debe apuntar a un PostgreSQL desechable en loopback")
    engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        existing = {row[0] for row in connection.execute(text("SELECT datname FROM pg_database"))}
    if existing & PROJECT_DATABASES:
        engine.dispose()
        pytest.skip("El servidor contiene bases del proyecto; use un PostgreSQL desechable")
    return engine


@pytest.fixture
def pg_url_factory():
    if not ADMIN_URL:
        pytest.skip("Opt-in: defina NEXUSTIX_TEST_PG_ADMIN_URL (PostgreSQL desechable)")
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    admin = _admin_engine()
    created = []

    def create() -> str:
        name = f"nxtest_{uuid.uuid4().hex[:12]}"
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)

    yield create
    with admin.connect() as connection:
        for name in created:
            assert name.startswith("nxtest_")
            connection.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                    "WHERE datname = :name AND pid <> pg_backend_pid()"), {"name": name})
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin.dispose()


@pytest.fixture
def keys(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "jwt_private.pem").write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (tmp_path / "jwt_public.pem").write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    (tmp_path / "service_token").write_text("pg-test-service-token\n")
    monkeypatch.setenv("JWT_PRIVATE_KEY_FILE", str(tmp_path / "jwt_private.pem"))
    monkeypatch.setenv("JWT_PUBLIC_KEY_FILE", str(tmp_path / "jwt_public.pem"))
    monkeypatch.setenv("EVENTOS_SERVICE_TOKEN_FILE", str(tmp_path / "service_token"))
    return tmp_path

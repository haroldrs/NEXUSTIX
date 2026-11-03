"""Shared JWT and service-to-service authentication helpers."""

import os
from pathlib import Path
from typing import Any

import jwt
from fastapi import Header, HTTPException
from jwt import InvalidTokenError

ISSUER = os.getenv("JWT_ISSUER", "nexustix-usuarios")
AUDIENCE = os.getenv("JWT_AUDIENCE", "nexustix-api")
JWT_ALGORITHM = "RS256"
ALLOWED_ROLES = {"comprador", "administrador", "organizador"}


def _read_key(env_name: str) -> str:
    key_path = os.getenv(env_name)
    if not key_path:
        raise RuntimeError(f"Falta configurar {env_name}")
    try:
        return Path(key_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"No se pudo leer el archivo configurado en {env_name}") from exc


def read_private_key() -> str:
    return _read_key("JWT_PRIVATE_KEY_FILE")


def read_public_key() -> str:
    return _read_key("JWT_PUBLIC_KEY_FILE")


def issue_access_token(subject: int, role: str, private_key: str, *, now: int | None = None) -> str:
    import time
    from uuid import uuid4

    issued_at = int(time.time()) if now is None else now
    claims = {
        "sub": str(subject),
        "role": role,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": issued_at,
        "exp": issued_at + int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "600")),
        "jti": uuid4().hex,
    }
    return jwt.encode(claims, private_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, public_key: str) -> dict[str, Any]:
    claims = jwt.decode(
        token,
        public_key,
        algorithms=[JWT_ALGORITHM],
        issuer=ISSUER,
        audience=AUDIENCE,
        options={"require": ["sub", "role", "iss", "aud", "iat", "exp", "jti"]},
    )
    if claims.get("role") not in ALLOWED_ROLES:
        raise InvalidTokenError("rol no válido")
    try:
        subject = int(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise InvalidTokenError("subject no válido") from exc
    if subject <= 0:
        raise InvalidTokenError("subject no válido")
    return claims


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Autenticación requerida", headers={"WWW-Authenticate": "Bearer"})
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Credencial Bearer inválida", headers={"WWW-Authenticate": "Bearer"})
    return token.strip()


def authenticated_claims(authorization: str | None) -> dict[str, Any]:
    token = bearer_token(authorization)
    try:
        return decode_access_token(token, read_public_key())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Autenticación no configurada correctamente") from exc
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Token inválido o expirado", headers={"WWW-Authenticate": "Bearer"}) from exc


def require_service_token(x_service_token: str | None = Header(default=None, alias="X-Service-Token")) -> None:
    """Fail closed when the internal service credential is absent or mismatched."""
    import hmac

    token_path = os.getenv("EVENTOS_SERVICE_TOKEN_FILE")
    if not token_path:
        raise HTTPException(status_code=503, detail="Autenticación entre servicios no configurada")
    try:
        expected = Path(token_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Credencial entre servicios no disponible") from exc
    if not expected or not x_service_token or not hmac.compare_digest(expected, x_service_token):
        raise HTTPException(status_code=401, detail="Credencial de servicio inválida")


def service_auth_headers() -> dict[str, str]:
    """Return the Eventos service credential without exposing it in logs."""
    try:
        token = Path(os.environ["EVENTOS_SERVICE_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    except (KeyError, OSError) as exc:
        raise RuntimeError("Credencial del servicio Eventos no configurada") from exc
    if not token:
        raise RuntimeError("Credencial del servicio Eventos vacía")
    from shared.telemetry import correlation_id
    return {"X-Service-Token": token, "X-Correlation-ID": correlation_id.get()}


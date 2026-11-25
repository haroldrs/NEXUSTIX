"""Códigos de un solo uso basados en tiempo (TOTP, RFC 6238).

Es el algoritmo de Google Authenticator y Microsoft Authenticator: el servidor y
el celular comparten un secreto; cada 30 s ambos calculan HMAC-SHA1(secreto,
número de intervalo) y lo reducen a 6 dígitos. El secreto nunca viaja después
de configurarlo, solo el código, que caduca en segundos.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1  # Acepta el intervalo anterior y el siguiente: tolera relojes desfasados ±30 s.


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def verify(secret: str, code: str, last_step: int | None = None, now: float | None = None) -> int | None:
    """Devuelve el intervalo aceptado o None.

    Un intervalo igual o anterior a ``last_step`` se rechaza: el mismo código no
    se puede reutilizar (protección contra reenvío / replay).
    """
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    base = current_step(now)
    for step in range(base - WINDOW, base + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "NEXUSTIX") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"

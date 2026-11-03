"""Pure validation for the staging-only Locust purchase profile."""

from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LocustSettings:
    users_url: str
    email: str = field(repr=False)
    password: str = field(repr=False)
    event_id: int


def _loopback_endpoint(value: str, expected_port: int, variable_name: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f"{variable_name} no es una URL válida") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or port != expected_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{variable_name} debe apuntar al endpoint loopback de staging en puerto {expected_port}")
    return f"http://{parsed.hostname}:{expected_port}"


def load_settings(host: str, environ: Mapping[str, str]) -> LocustSettings:
    """Load credentials only after proving both HTTP targets are staging loopback."""
    if environ.get("NEXUSTIX_STAGING_HTTP") != "1":
        raise ValueError("Define NEXUSTIX_STAGING_HTTP=1 para habilitar la carga en staging")
    _loopback_endpoint(host, 18085, "Locust --host")
    users_url = _loopback_endpoint(environ.get("NEXUSTIX_LOCUST_USERS_URL", ""), 18081,
                                   "NEXUSTIX_LOCUST_USERS_URL")
    email = environ.get("NEXUSTIX_LOCUST_EMAIL", "").strip()
    password = environ.get("NEXUSTIX_LOCUST_PASSWORD", "")
    try:
        event_id = int(environ.get("NEXUSTIX_LOCUST_EVENT_ID", ""))
    except ValueError:
        raise ValueError("NEXUSTIX_LOCUST_EVENT_ID debe ser un ID sintético entero") from None
    if not email or not password or event_id <= 0:
        raise ValueError("Configura credenciales sintéticas y un evento de staging con inventario controlado")
    return LocustSettings(users_url=users_url, email=email, password=password, event_id=event_id)

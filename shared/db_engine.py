"""Shared SQLAlchemy engine settings for the three API services.

The URL may contain several PostgreSQL hosts (``?host=a:5432&host=b:5432``)
together with ``target_session_attrs=read-write``: libpq then connects only to
the node that currently accepts writes, which is how the services follow a
controlled PostgreSQL promotion (see docker-compose.replicacion.yml).
"""

import os
from pathlib import Path


def read_database_url() -> str:
    """Read DATABASE_URL_FILE (preferred, Docker secret) or DATABASE_URL."""
    url_file = os.getenv("DATABASE_URL_FILE")
    if url_file:
        try:
            url = Path(url_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("No se pudo leer el secreto configurado en DATABASE_URL_FILE") from exc
        if not url:
            raise RuntimeError("El secreto DATABASE_URL_FILE está vacío")
        return url
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Configure DATABASE_URL_FILE o DATABASE_URL; no existe conexión por defecto")
    return url


def engine_options(url: str) -> dict:
    """Pool options that survive PostgreSQL restarts and failovers.

    pool_pre_ping discards pooled connections that died while PostgreSQL was
    restarted or promoted, instead of failing the next request with a 500.
    connect_timeout bounds each host attempt so an unreachable node does not
    block a request for the operating-system TCP timeout.
    """
    if not url.startswith("postgresql"):
        return {}
    connect_args = {
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "5")),
        # TCP keepalive de libpq: detecta en ~25 s un servidor que desapareció
        # sin cerrar la conexión (host caído, partición de red).
        "keepalives": 1,
        "keepalives_idle": 10,
        "keepalives_interval": 5,
        "keepalives_count": 3,
    }
    application_name = os.getenv("DB_APPLICATION_NAME")
    if application_name:
        connect_args["application_name"] = application_name
    return {
        "pool_pre_ping": True,
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")),
        "connect_args": connect_args,
    }

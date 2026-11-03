#!/usr/bin/env python3
"""Prepara datos SINTÉTICOS para Locust en el entorno de pruebas (nunca en demo).

    python3 scripts/preparar_carga.py --stock 300
    source ~/.config/nexustix-locust.env

Crea un evento con id aleatorio alto y stock_inicial propio (no altera los
eventos base) y un comprador qa-carga-*@example.com. Escribe las variables
para Locust en ~/.config/nexustix-locust.env (modo 0600); la contraseña no se
muestra. Repetir el script crea un evento nuevo: las ejecuciones de carga no
se mezclan y no hace falta borrar datos previos.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entornos  # noqa: E402
import pg_docker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stock", type=int, default=300)
    parser.add_argument("--salida", type=Path, default=Path("~/.config/nexustix-locust.env"))
    args = parser.parse_args()
    if not 1 <= args.stock <= 100000:
        parser.error("--stock entre 1 y 100000")
    entorno = entornos.get("pruebas")
    event_id = int(uuid.uuid4().int % 1_900_000_000) + 10_000_000
    eventos = pg_docker.primario(entorno, "eventos_db")
    pg_docker.psql(eventos.container, "eventos_db",
                   f"INSERT INTO eventos (id, nombre, stock, stock_inicial) VALUES "
                   f"({event_id}, 'QA carga {event_id}', {args.stock}, {args.stock})")
    email = f"qa-carga-{uuid.uuid4().hex[:10]}@example.com"
    password = secrets.token_urlsafe(24)
    request = urllib.request.Request(
        f"{entorno.url('usuarios')}/auth/register", method="POST",
        data=json.dumps({"nombre": "QA carga", "email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 201:
            raise SystemExit(f"No se pudo crear el comprador sintético (HTTP {response.status})")
    output = args.salida.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "NEXUSTIX_STAGING_HTTP": "1",
        "NEXUSTIX_LOCUST_USERS_URL": entorno.url("usuarios"),
        "NEXUSTIX_LOCUST_EMAIL": email,
        "NEXUSTIX_LOCUST_PASSWORD": password,
        "NEXUSTIX_LOCUST_EVENT_ID": str(event_id),
        "NEXUSTIX_LOCUST_ADMIN_EMAIL": os.getenv("NEXUSTIX_STAGING_ADMIN_EMAIL", "qa-admin@example.com"),
        "NEXUSTIX_LOCUST_ADMIN_PASSWORD_FILE": str(entorno.secrets_dir / "admin-password"),
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.writelines(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())
    os.chmod(output, 0o600)
    print(f"Evento sintético {event_id} con stock {args.stock}; comprador {email}.")
    print(f"Variables en {output} (0600). Use: source {shlex.quote(str(output))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

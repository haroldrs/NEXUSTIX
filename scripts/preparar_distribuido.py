#!/usr/bin/env python3
"""Genera secretos NUEVOS para laboratorio o VMs; no ejecuta Docker ni red."""
import argparse
from pathlib import Path
import re
import secrets
from urllib.parse import quote

from generar_secretos import generate_rsa_pair, write_secret


def generate(directory, *, primary=None, standby=None, public_host="localhost", vm=False):
    directory = Path(directory).expanduser().resolve()
    if directory.exists():
        raise ValueError("La carpeta ya existe: no se sobrescriben secretos")
    for host in (primary, standby, public_host):
        if host and not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ValueError("Host inválido")
    if vm and not (primary and standby):
        raise ValueError("VM requiere hosts de primario y standby")
    directory.mkdir(mode=0o700, parents=True)
    generate_rsa_pair(directory / "jwt-private.pem", directory / "jwt-public.pem")
    for index, service in enumerate(("usuarios", "eventos", "compras")):
        password = secrets.token_urlsafe(32)
        port = 5432 + index if vm else 5432
        hosts = [primary, standby] if vm else [f"{service}_db", f"{service}_db_replica"]
        url = f"postgresql://postgres:{quote(password, safe='')}@/{service}_db?" + "&".join(f"host={h}:{port}" for h in hosts) + "&target_session_attrs=read-write"
        write_secret(directory / f"{service}-db-password", password)
        write_secret(directory / f"{service}-database-url", url)
    for file in ("eventos-service-token", "replication-password", "admin-password"):
        write_secret(directory / file, secrets.token_urlsafe(40))
    ports = dict(USUARIOS_URL=8001, EVENTOS_URL=8002, COMPRAS_R1_URL=8003, COMPRAS_R2_URL=8004, COMPRAS_LB_URL=8005) if vm else dict(USUARIOS_URL=28081, EVENTOS_URL=28082, COMPRAS_R1_URL=28083, COMPRAS_R2_URL=28084, COMPRAS_LB_URL=28085)
    import json
    config = {name: f"http://{public_host}:{port}" for name, port in ports.items()}
    config["CHECKOUT_ORDERS"] = True
    write_secret(directory / "frontend-config.js", "const CONFIG = Object.freeze(" + json.dumps(config) + ");\n", mode=0o644)
    print("Secretos nuevos creados; no se muestran valores:", directory)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--vm", action="store_true")
    p.add_argument("--primary"); p.add_argument("--standby")
    p.add_argument("--public-host", default="localhost")
    a = p.parse_args()
    generate(a.directory, primary=a.primary, standby=a.standby, public_host=a.public_host, vm=a.vm)


if __name__ == "__main__":
    main()

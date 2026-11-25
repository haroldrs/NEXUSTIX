#!/usr/bin/env python3
"""Paquete de imágenes para instalar NEXUSTIX sin descargar nada en el laboratorio.

En casa (con buena conexión):
    python3 scripts/paquete_offline.py crear --salida ~/nexustix-imagenes.tar
En cada PC del laboratorio (desde un USB):
    python3 scripts/paquete_offline.py cargar --entrada /media/usb/nexustix-imagenes.tar

`crear` construye las imágenes de los servicios, PostgreSQL con Patroni, etcd,
HAProxy y Nginx, y las guarda en un único archivo con `docker save`.
`cargar` las importa con `docker load`. Los Compose generados por
despliegue-nodos usan estos mismos nombres, así que `docker compose up` ya no
necesita Internet.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "despliegue-nodos"))

BUILDS = {
    "nexustix/usuarios:offline": "usuarios/Dockerfile",
    "nexustix/eventos:offline": "eventos/Dockerfile",
    "nexustix/compras:offline": "compras/Dockerfile",
    "nexustix/frontend:offline": "frontend/Dockerfile",
    "nexustix/patroni:offline": "despliegue-nodos/patroni/Dockerfile",
    "nexustix/postgres:offline": "despliegue-nodos/postgres/Dockerfile",
}
PULLS = ["haproxy:3.0-alpine", "gcr.io/etcd-development/etcd:v3.5.17"]


def run(*args):
    print("$", " ".join(args), flush=True)
    subprocess.run(args, check=True)


def create(output: Path):
    for image in PULLS:
        run("docker", "pull", image)
    for tag, dockerfile in BUILDS.items():
        run("docker", "build", "-t", tag, "-f", str(ROOT / dockerfile), str(ROOT))
    run("docker", "save", "-o", str(output), *PULLS, *BUILDS)
    print(f"Paquete listo: {output} ({output.stat().st_size / 2**30:.2f} GiB). Cópielo a un USB.")


def load(source: Path):
    run("docker", "load", "-i", str(source))
    print("Imágenes cargadas. Use despliegue-nodos/nodos.py con --imagenes-offline para no reconstruir.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="accion", required=True)
    sub.add_parser("crear").add_argument("--salida", required=True, type=Path)
    sub.add_parser("cargar").add_argument("--entrada", required=True, type=Path)
    args = parser.parse_args()
    if args.accion == "crear":
        create(args.salida.expanduser().resolve())
    else:
        load(args.entrada.expanduser().resolve())


if __name__ == "__main__":
    main()

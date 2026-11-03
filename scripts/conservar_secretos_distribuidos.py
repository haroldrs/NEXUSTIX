#!/usr/bin/env python3
"""Copia los secretos del laboratorio desde /tmp a una carpeta privada NUEVA."""
import argparse
from pathlib import Path
import os
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.expanduser().resolve()
    if not source.is_dir() or destination.exists():
        parser.error("Origen requerido y destino nuevo; no se sobrescribe nada")
    expected = {"jwt-private.pem", "jwt-public.pem", "eventos-service-token", "admin-password",
                "replication-password", "frontend-config.js"}
    expected.update(f"{s}-{kind}" for s in ("usuarios", "eventos", "compras") for kind in ("db-password", "database-url"))
    if {p.name for p in source.iterdir()} != expected or any(p.is_symlink() or not p.is_file() for p in source.iterdir()):
        parser.error("Conjunto de secretos no reconocido")
    os.umask(0o077)
    destination.mkdir(parents=True, mode=0o700)
    for name in expected:
        shutil.copyfile(source / name, destination / name)
        (destination / name).chmod(0o644 if name == "frontend-config.js" else 0o600)
        if (source / name).read_bytes() != (destination / name).read_bytes():
            raise RuntimeError("Copia no verificada")
    print("Copia verificada en carpeta privada nueva; origen conservado:", destination)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Identidad individual y anuncios firmados offline; no instala ni contacta hosts."""
import argparse
import base64
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.node_admission import AnnouncementError, canonical, verify


def outside(path):
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise AnnouncementError("Identidades y anuncios deben permanecer fuera del repositorio")
    return path


def write_new(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def public_values(key):
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(public).decode("ascii"), hashlib.sha256(public).hexdigest()


def identity(directory):
    directory = outside(directory)
    if directory.exists():
        raise AnnouncementError("Use un directorio NUEVO para la identidad; no se sobrescriben claves")
    directory.mkdir(parents=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    encoded, fingerprint = public_values(key)
    write_new(directory / "node-identity.pem", key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    write_new(directory / "node-public.json", (json.dumps({"public_key": encoded, "identity_fingerprint": fingerprint}, indent=2) + "\n").encode())
    print(f"Identidad creada en {directory}")
    print(f"Huella pública SHA256: {fingerprint}")
    print("Conserve node-identity.pem sólo en esta PC. Verifique la huella por un canal independiente.")


def sign(args):
    key_path = args.key.expanduser()
    if key_path.is_symlink():
        raise AnnouncementError("La clave debe ser un archivo regular privado")
    key_path = outside(key_path)
    if not key_path.is_file() or key_path.stat().st_mode & 0o077 or key_path.stat().st_size > 4096:
        raise AnnouncementError("Se requiere una clave privada regular con permisos 0600")
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise AnnouncementError("La clave debe ser Ed25519, distinta de las claves JWT")
    output = outside(args.output)
    if output.exists():
        raise AnnouncementError("El archivo de salida ya existe; indique un nombre nuevo")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.parent.stat().st_mode & 0o077:
        raise AnnouncementError("Guarde el anuncio en un directorio privado 0700")
    cpu = Decimal(args.cpus)
    if not cpu.is_finite() or cpu * 1000 != (cpu * 1000).to_integral_value():
        raise AnnouncementError("CPU requiere un número positivo con hasta tres decimales")
    issued = int(time.time())
    payload = {"deployment": args.deployment, "node_id": args.node_id, "address": args.address,
        "platform": args.platform, "failure_domain": args.failure_domain,
        "resources": {"cpu_millicores": int(cpu * 1000), "memory_mb": args.memory_mb},
        "release": args.release, "issued_at": issued, "expires_at": issued + args.ttl,
        "nonce": uuid4().hex}
    public, fingerprint = public_values(key)
    envelope = {"version": 1, "public_key": public, "payload": payload,
        "signature": base64.b64encode(key.sign(canonical(payload))).decode("ascii")}
    verify(envelope, args.deployment)
    write_new(output, (json.dumps(envelope, indent=2, ensure_ascii=True) + "\n").encode())
    print(f"Anuncio firmado: {output}")
    print(f"Huella pública SHA256: {fingerprint}")
    print(f"Vigencia: {args.ttl} segundos. Importe sólo este JSON desde el Centro de Control.")
    print("Los recursos son declarados; no se midió el host ni se enviaron peticiones de red.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("identidad", help="Crea una clave individual en un directorio nuevo y privado")
    create.add_argument("directory", type=Path)
    announce = sub.add_parser("firmar", help="Prepara un anuncio firmado para transferencia manual")
    announce.add_argument("--key", type=Path, required=True)
    announce.add_argument("--output", type=Path, required=True)
    announce.add_argument("--deployment", required=True)
    announce.add_argument("--node-id", required=True)
    announce.add_argument("--address", required=True)
    announce.add_argument("--platform", choices=("fedora", "wsl2", "linux"), required=True)
    announce.add_argument("--failure-domain", required=True)
    announce.add_argument("--cpus", required=True)
    announce.add_argument("--memory-mb", type=int, required=True)
    announce.add_argument("--release", default="sin-etiqueta")
    announce.add_argument("--ttl", type=int, default=900)
    args = parser.parse_args()
    previous = os.umask(0o077)
    try:
        identity(args.directory) if args.command == "identidad" else sign(args)
    except (OSError, ValueError, TypeError, InvalidOperation) as error:
        message = str(error) if isinstance(error, AnnouncementError) else "No se pudo preparar la identidad/anuncio; revisar rutas, permisos y formato de clave"
        parser.exit(2, message + "\n")
    finally:
        os.umask(previous)


if __name__ == "__main__":
    main()

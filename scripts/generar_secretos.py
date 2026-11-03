#!/usr/bin/env python3
"""Genera, en una carpeta nueva fuera del repositorio, los secretos de un entorno NEXUSTIX.

    python3 scripts/generar_secretos.py --entorno demo
    python3 scripts/generar_secretos.py --entorno pruebas --directory ~/.config/nexustix-auth-staging-secrets

Crea claves JWT RS256 (3072 bits), contraseñas de PostgreSQL y de replicación,
la credencial interna Compras→Eventos, las URL de conexión (simple y HA) y el
config.js del frontend. Nunca sobrescribe ni muestra valores: si la carpeta ya
existe, se detiene. En Fedora con SELinux en modo Enforcing etiqueta la carpeta
como container_file_t para que Docker pueda montar los secretos.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from urllib.parse import quote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entornos  # noqa: E402

ROOT = entornos.ROOT


def write_secret(path: Path, value: str, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
    os.chmod(path, mode)


def generate_rsa_pair(private_key: Path, public_key: Path) -> None:
    if shutil.which("openssl"):
        with private_key.open("xb") as output:
            os.chmod(private_key, 0o600)
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072"],
                           stdout=output, stderr=subprocess.DEVNULL, check=True)
        with public_key.open("xb") as output:
            os.chmod(public_key, 0o600)
            subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout"],
                           stdout=output, stderr=subprocess.DEVNULL, check=True)
        return
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        raise SystemExit("Se requiere openssl (paquete 'openssl' en Fedora) o el módulo cryptography") from None
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    write_secret(private_key, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()).decode())
    write_secret(public_key, key.public_key().public_bytes(serialization.Encoding.PEM,
                                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode())


def database_urls(service: str, password: str) -> tuple[str, str]:
    database = f"{service}_db"
    password = quote(password, safe="")
    simple = f"postgresql://postgres:{password}@{database}:5432/{database}"
    # libpq prueba los nodos en orden y solo acepta el que admite escrituras;
    # así los servicios siguen al primario tras una promoción controlada.
    ha = (f"postgresql://postgres:{password}@/{database}"
          f"?host={database}:5432&host={database}_replica:5432&target_session_attrs=read-write")
    return simple, ha


def frontend_config(entorno: entornos.Entorno, public_host: str) -> str:
    ports = entorno.puertos
    return (
        "// Generado por scripts/generar_secretos.py; URLs públicas de las APIs (no contiene secretos).\n"
        "const CONFIG = Object.freeze({\n"
        f'  USUARIOS_URL: "http://{public_host}:{ports["usuarios"]}",\n'
        f'  EVENTOS_URL: "http://{public_host}:{ports["eventos"]}",\n'
        f'  COMPRAS_R1_URL: "http://{public_host}:{ports["compras1"]}",\n'
        f'  COMPRAS_R2_URL: "http://{public_host}:{ports["compras2"]}",\n'
        f'  COMPRAS_LB_URL: "http://{public_host}:{ports["compras_lb"]}",\n'
        "});\n"
    )


def label_for_selinux(directory: Path) -> str:
    getenforce = shutil.which("getenforce")
    if not getenforce:
        return "SELinux no detectado; no se aplicó etiqueta."
    mode = subprocess.run([getenforce], capture_output=True, text=True, check=False).stdout.strip()
    if mode != "Enforcing":
        return f"SELinux en modo {mode or 'desconocido'}; no se aplicó etiqueta."
    result = subprocess.run(["chcon", "-R", "-t", "container_file_t", str(directory)],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        return ("AVISO: SELinux Enforcing y no se pudo etiquetar la carpeta. Ejecute: "
                f"chcon -R -t container_file_t {shlex.quote(str(directory))}")
    return "SELinux Enforcing: carpeta etiquetada como container_file_t."


def generate(entorno: entornos.Entorno, directory: Path, public_host: str, legacy_db_password: bool) -> list[str]:
    legacy_password = None
    if legacy_db_password:
        source = os.getenv("NEXUSTIX_LEGACY_DB_PASSWORD_FILE")
        if not source or not Path(source).is_file():
            raise ValueError("Compatibilidad legacy requiere NEXUSTIX_LEGACY_DB_PASSWORD_FILE existente")
        legacy_password = Path(source).read_text().strip()
        if not legacy_password:
            raise ValueError("Archivo de credencial legacy vacío")
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory, 0o700)
    try:
        generate_rsa_pair(directory / "jwt-private.pem", directory / "jwt-public.pem")
        for service in ("usuarios", "eventos", "compras"):
            # La compatibilidad exige una credencial externa explícita; nunca una fija.
            password = legacy_password if legacy_db_password else secrets.token_urlsafe(32)
            simple, ha = database_urls(service, password)
            write_secret(directory / f"{service}-db-password", password)
            write_secret(directory / f"{service}-database-url", simple)
            write_secret(directory / f"{service}-database-url-ha", ha)
        write_secret(directory / "replication-password", secrets.token_urlsafe(32))
        write_secret(directory / "eventos-service-token", secrets.token_urlsafe(48))
        write_secret(directory / "admin-password", secrets.token_urlsafe(24))
        # Nginx sirve este archivo con un worker sin privilegios; solo contiene URLs públicas.
        write_secret(directory / "frontend-config.js", frontend_config(entorno, public_host), mode=0o644)
    except BaseException:
        # Solo se elimina la carpeta recién creada por este proceso.
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
        raise
    created = sorted(path.name for path in directory.iterdir())
    if set(created) != set(entornos.SECRET_FILES):
        raise SystemExit(f"Conjunto de secretos inesperado: {created}")
    return created


def complete(entorno: entornos.Entorno, directory: Path, public_host: str) -> list[str]:
    """Añade a una carpeta de secretos EXISTENTE solo los archivos que faltan.

    Para instalaciones previas (p. ej. el staging de la versión anterior): las
    contraseñas de base ya inicializadas en los volúmenes se reutilizan para
    construir las URL nuevas; nada existente se modifica.
    """
    present = {path.name for path in directory.iterdir()}
    unknown = present - set(entornos.SECRET_FILES)
    if unknown:
        raise SystemExit(f"La carpeta contiene archivos inesperados ({', '.join(sorted(unknown))}); no se toca")
    added: list[str] = []

    def add(name: str, value: str, mode: int = 0o600) -> None:
        if name not in present:
            write_secret(directory / name, value, mode)
            added.append(name)

    jwt = {"jwt-private.pem", "jwt-public.pem"} & present
    if len(jwt) == 1:
        raise SystemExit("Solo existe una de las dos claves JWT; revise la carpeta manualmente")
    if not jwt:
        generate_rsa_pair(directory / "jwt-private.pem", directory / "jwt-public.pem")
        added += ["jwt-private.pem", "jwt-public.pem"]
    for service in ("usuarios", "eventos", "compras"):
        password_file = directory / f"{service}-db-password"
        if password_file.exists():
            password = password_file.read_text(encoding="utf-8").strip()
        elif f"{service}-database-url" in present:
            raise SystemExit(f"Existe {service}-database-url sin {service}-db-password; revise la carpeta manualmente")
        else:
            password = secrets.token_urlsafe(32)
        simple, ha = database_urls(service, password)
        add(f"{service}-db-password", password)
        add(f"{service}-database-url", simple)
        add(f"{service}-database-url-ha", ha)
    add("replication-password", secrets.token_urlsafe(32))
    add("eventos-service-token", secrets.token_urlsafe(48))
    add("admin-password", secrets.token_urlsafe(24))
    add("frontend-config.js", frontend_config(entorno, public_host), mode=0o644)
    return sorted(added)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entorno", required=True, choices=sorted(entornos.ENTORNOS))
    parser.add_argument("--directory", type=Path, help="Carpeta nueva fuera del repositorio (por defecto la del entorno)")
    parser.add_argument("--public-host", default="localhost",
                        help="Host con el que el navegador alcanza las APIs (IP de la PC en despliegues de red)")
    parser.add_argument("--legacy-db-password", action="store_true",
                        help="Compatibilidad: exige NEXUSTIX_LEGACY_DB_PASSWORD_FILE; no cambia bases existentes")
    parser.add_argument("--completar", action="store_true",
                        help="Carpeta existente de una instalación previa: añade solo los archivos que faltan")
    args = parser.parse_args(argv)

    entorno = entornos.get(args.entorno)
    directory = (args.directory.expanduser() if args.directory else entorno.secrets_dir).resolve()
    if directory == ROOT or ROOT in directory.parents:
        parser.error("La carpeta de secretos debe estar fuera del repositorio")
    if directory in {e.secrets_dir for e in entornos.ENTORNOS.values() if e.nombre != entorno.nombre}:
        parser.error("Esa carpeta pertenece a otro entorno; cada entorno usa secretos distintos")
    if args.completar:
        if not directory.is_dir():
            parser.error(f"--completar requiere una carpeta existente: {directory}")
        added = complete(entorno, directory, args.public_host)
        print(f"Añadidos {len(added)} archivos que faltaban ({', '.join(added) or 'ninguno'}); "
              "los existentes no se modificaron ni se mostraron.")
        print(label_for_selinux(directory))
        return 0
    if directory.exists():
        parser.error(f"La carpeta {directory} ya existe; el script no sobrescribe ni mezcla secretos "
                     "(para una instalación previa use --completar)")
    other = {e.secrets_dir for e in entornos.ENTORNOS.values() if e.nombre != entorno.nombre}
    if directory in other:
        parser.error("Esa carpeta pertenece a otro entorno; cada entorno usa secretos distintos")
    if not all(ch.isalnum() or ch in ".-:[]" for ch in args.public_host):
        parser.error("--public-host debe ser un nombre de host o una IP")

    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = generate(entorno, directory, args.public_host, args.legacy_db_password)
    print(f"Secretos del entorno '{entorno.nombre}' creados ({len(created)} archivos, modo 0600); no se mostraron valores.")
    print(label_for_selinux(directory))
    print(f"Carpeta: {directory}")
    if directory != entorno.secrets_dir:
        print(f"Antes de usar el entorno: export {entorno.secrets_env}={shlex.quote(str(directory))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

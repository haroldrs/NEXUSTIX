#!/usr/bin/env python3
"""Backup físico verificable y preparación de PITR en un directorio NUEVO.

Usa PGHOST/PGPORT/PGUSER/PGPASSFILE (libpq). No recibe contraseñas por CLI.
No arranca PostgreSQL ni sustituye un clúster existente.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess


def binary(directory, name):
    return str(Path(directory) / name) if directory else name


def backup(destination, pg_bin=None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("El destino ya existe; no se sobrescribe")
    destination.mkdir(parents=True, mode=0o700)
    subprocess.run([binary(pg_bin, "pg_basebackup"), "-D", str(destination / "base"),
        "-X", "stream", "--checkpoint=fast", "--manifest-checksums=SHA256"], check=True)
    subprocess.run([binary(pg_bin, "pg_verifybackup"), str(destination / "base")], check=True)
    (destination / "verification.json").write_text(json.dumps({"verified": True,
        "created_utc": datetime.now(timezone.utc).isoformat(), "method": "pg_basebackup + pg_verifybackup",
        "wal_external_required_for_pitr": True}, indent=2))
    return destination / "base"


def restore(source, destination, archive, target, pg_bin=None):
    source, destination, archive = map(lambda p: Path(p).resolve(), (source, destination, archive))
    if destination.exists():
        raise ValueError("La restauración exige un directorio que no exista")
    if not archive.is_dir() or not target or '\n' in target:
        raise ValueError("Archivo WAL y punto de restauración explícitos requeridos")
    subprocess.run([binary(pg_bin, "pg_verifybackup"), str(source)], check=True)
    shutil.copytree(source, destination)
    destination.chmod(0o700)
    # Impide seguir a un primario accidentalmente desde un backup de standby.
    (destination / "standby.signal").unlink(missing_ok=True)
    import shlex
    restore_command = "cp " + shlex.quote(str(archive)) + "/%f %p"
    literal = lambda value: "'" + value.replace("'", "''") + "'"
    with (destination / "postgresql.auto.conf").open("a") as config:
        config.write("\nprimary_conninfo = ''\narchive_mode = 'off'\n")
        config.write("restore_command = " + literal(restore_command) + "\n")
        config.write("recovery_target_name = " + literal(target) + "\nrecovery_target_action = 'promote'\n")
    (destination / "recovery.signal").touch(mode=0o600)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup"); b.add_argument("destination")
    r = sub.add_parser("restore"); r.add_argument("source"); r.add_argument("destination")
    r.add_argument("--archive", required=True); r.add_argument("--target-name", required=True)
    args = parser.parse_args()
    if args.command == "backup":
        backup(args.destination, args.pg_bin)
    else:
        restore(args.source, args.destination, args.archive, args.target_name, args.pg_bin)


if __name__ == "__main__":
    main()

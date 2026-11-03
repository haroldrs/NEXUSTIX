#!/usr/bin/env python3
"""Remove only the explicitly confirmed external NEXUSTIX staging secret directory."""

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entornos  # noqa: E402

ROOT = entornos.ROOT
EXPECTED_FILES = set(entornos.SECRET_FILES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--confirm", required=True, help="Must be exactly nexustix-auth-staging")
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    if args.confirm != "nexustix-auth-staging":
        parser.error("Confirma explícitamente el nombre nexustix-auth-staging")
    if directory == ROOT or ROOT in directory.parents or not directory.is_dir():
        parser.error("Se rechaza un directorio inexistente o dentro del repositorio")
    actual = {path.name for path in directory.iterdir()}
    if actual != EXPECTED_FILES or any(path.is_dir() for path in directory.iterdir()):
        parser.error("El directorio no coincide con el conjunto de archivos creado para este staging; no se borró")
    shutil.rmtree(directory)
    print("Se eliminaron únicamente los archivos de secretos del directorio staging confirmado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

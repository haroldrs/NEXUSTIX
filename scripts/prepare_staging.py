#!/usr/bin/env python3
"""Compatibilidad: genera los secretos del entorno de pruebas (staging).

Equivale a ``python3 scripts/generar_secretos.py --entorno pruebas --directory DIR``.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generar_secretos  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path,
                        help="New directory outside the project for staging secrets")
    args = parser.parse_args()
    return generar_secretos.main(["--entorno", "pruebas", "--directory", str(args.directory)])


if __name__ == "__main__":
    raise SystemExit(main())

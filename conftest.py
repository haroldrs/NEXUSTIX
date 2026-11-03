"""Opt-in adicional para suites que pueden intervenir infraestructura real."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


def pytest_addoption(parser):
    parser.addoption(
        "--allow-infrastructure-tests", action="store_true", default=False,
        help="Permite recoger pruebas de PostgreSQL/Docker/HTTP; revisar destino y efectos antes.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--allow-infrastructure-tests"):
        return
    for item in items:
        path = Path(item.path).resolve()
        if not path.is_relative_to(ROOT / "tests"):
            continue
        relative = path.relative_to(ROOT / "tests")
        if relative.parts[0] in {"postgres", "resiliencia", "staging"} or relative.name == "test_backup_restore_isolated.py":
            raise pytest.UsageError(
                "Suite de infraestructura bloqueada antes de ejecutar fixtures: "
                "requiere --allow-infrastructure-tests y sus opt-in originales. "
                "Consulte docs/PRUEBAS_Y_EVIDENCIAS.md."
            )

"""Ayudas para importar servicios contra un PostgreSQL desechable (sin fixtures)."""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_service(service: str, url: str, monkeypatch):
    """Importa main/models/database de un servicio contra ``url`` (módulos frescos)."""
    for name in ("main", "models", "database", "seed"):
        sys.modules.pop(name, None)
    for other in ("usuarios", "eventos", "compras"):
        path = str(ROOT / other)
        while path in sys.path:
            sys.path.remove(path)
    sys.path.insert(0, str(ROOT / service))
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("DATABASE_URL", url)
    database = importlib.import_module("database")
    models = importlib.import_module("models")
    main = importlib.import_module("main")
    return main, models, database


def migrate(service: str, url: str, monkeypatch) -> int:
    from shared import run_migrations

    monkeypatch.chdir(ROOT / service)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("NEXUSTIX_AUTO_MIGRATE", "1")
    return run_migrations.main()

"""Política de migraciones protegidas (shared/run_migrations.py).

La matriz de decisión se prueba de forma pura. La ejecución real de Alembic
se ensaya con el env.py del proyecto y una migración trivial sobre SQLite
temporal; las migraciones reales (SQL de PostgreSQL) se prueban contra
PostgreSQL en tests/postgres y en el entorno Docker de pruebas.
"""
import shutil
import sqlite3
import textwrap
from pathlib import Path

import pytest

from shared import run_migrations

ROOT = Path(__file__).resolve().parents[1]
KNOWN = {"20261022_00", "20261022_01", "20261023_02"}


@pytest.mark.parametrize("tables,current,allow,expected", [
    (set(), None, False, "upgrade"),                                # instalación limpia
    ({"compras", "journal_compras"}, None, False, "refuse"),        # legacy sin aprobación
    ({"compras", "journal_compras"}, None, True, "upgrade"),        # legacy aprobado tras respaldo
    ({"alembic_version", "compras"}, "20261022_01", False, "upgrade"),  # actualización
    ({"alembic_version", "compras"}, "20261023_02", False, "noop"),     # ya en head
    ({"alembic_version", "compras"}, "29990101_99", False, "refuse"),   # revisión desconocida
    ({"alembic_version"}, None, False, "upgrade"),                   # tabla de versión vacía
])
def test_decision_matrix(tables, current, allow, expected):
    action, message = run_migrations.decide_action(tables, current, KNOWN, "20261023_02", allow)
    assert action == expected, message


def test_migrations_are_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("NEXUSTIX_AUTO_MIGRATE", "0")
    assert run_migrations.main() == 0


def _alembic_project(tmp_path: Path) -> Path:
    project = tmp_path / "svc"
    (project / "migrations" / "versions").mkdir(parents=True)
    shutil.copy(ROOT / "compras" / "migrations" / "env.py", project / "migrations" / "env.py")
    shutil.copy(ROOT / "compras" / "migrations" / "script.py.mako", project / "migrations" / "script.py.mako")
    shutil.copy(ROOT / "compras" / "alembic.ini", project / "alembic.ini")
    (project / "migrations" / "versions" / "0001_demo.py").write_text(textwrap.dedent('''
        from alembic import op
        revision = "0001"
        down_revision = None
        branch_labels = None
        depends_on = None

        def upgrade():
            op.execute("CREATE TABLE demo_items (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)")

        def downgrade():
            pass
    '''), encoding="utf-8")
    return project


def test_shared_connection_upgrade_creates_schema_once(tmp_path, monkeypatch):
    project = _alembic_project(tmp_path)
    database = tmp_path / "migracion.sqlite"
    monkeypatch.chdir(project)
    monkeypatch.setenv("NEXUSTIX_AUTO_MIGRATE", "1")
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    assert run_migrations.main() == 0
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"demo_items", "alembic_version"} <= tables
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001"
    # Segunda ejecución (reinicio del contenedor): no-op, no falla ni duplica.
    assert run_migrations.main() == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM alembic_version").fetchone()[0] == 1


def test_legacy_database_without_version_is_refused(tmp_path, monkeypatch):
    project = _alembic_project(tmp_path)
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE compras (id INTEGER PRIMARY KEY)")
    monkeypatch.chdir(project)
    monkeypatch.setenv("NEXUSTIX_AUTO_MIGRATE", "1")
    monkeypatch.delenv("NEXUSTIX_ALLOW_LEGACY_BASELINE", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    assert run_migrations.main() == 4
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"compras"}, "no se modificó la base legacy"


def test_service_images_run_guarded_migrations_before_uvicorn():
    for service in ("usuarios", "compras"):
        dockerfile = (ROOT / service / "Dockerfile").read_text(encoding="utf-8")
        assert "python -m shared.run_migrations && exec uvicorn" in dockerfile
    assert "run_migrations" not in (ROOT / "eventos" / "Dockerfile").read_text(encoding="utf-8")


def test_env_py_has_no_hardcoded_credentials():
    for service in ("usuarios", "compras"):
        env_py = (ROOT / service / "migrations" / "env.py").read_text(encoding="utf-8")
        assert "postgres:postgres@" not in env_py

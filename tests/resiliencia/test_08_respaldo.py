"""R20 · Respaldo del entorno de pruebas y restauración en PostgreSQL temporal aislado.

Nunca restaura sobre las bases originales: 'verify' crea una red interna y un
PostgreSQL en tmpfs, restaura allí y compara agregados, filas por tabla y
esquema (tablas, columnas, restricciones, FKs, índices y revisión Alembic).
Limpieza: el directorio de respaldo queda en el tmp de pytest como evidencia.
"""
import json
import subprocess
import sys

import nx_pruebas as nx


def test_r20_respaldo_verificado_y_restaurado_en_aislamiento(tmp_path):
    """Esperado: nx.py pruebas respaldo termina 0; manifest 'complete' con SHA-256 y esquema por
    base; verification.json 'verified' con counts_match y schema_match en las tres bases; los
    recursos temporales se eliminan. Criterio: todo lo anterior y el proyecto de pruebas sigue sano."""
    destination = tmp_path / "respaldo-pruebas"
    result = subprocess.run([sys.executable, str(nx.ROOT / "scripts" / "nx.py"), "pruebas", "respaldo",
                             "--destino", str(destination), "--ensayar-migraciones"],
                            cwd=nx.ROOT, capture_output=True, text=True, timeout=3600)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["status"] == "complete" and manifest["compose_project"] == "nexustix-auth-staging"
    for name, item in manifest["databases"].items():
        assert len(item["sha256"]) == 64 and item["before"]["schema"]["tables"], name
    verification = json.loads((destination / "verification.json").read_text())
    assert verification["status"] == "verified" and verification["temporary_resources"] == "removed"
    assert verification["migrations_rehearsed"] is True
    assert all(db["counts_match"] and db["schema_match"] for db in verification["databases"].values())
    nx.all_healthy()

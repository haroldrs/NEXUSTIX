"""Basebackup + WAL: restaura un punto nombrado en otro clúster real."""
import importlib.util
import os
from pathlib import Path
import subprocess
import time

import pytest
from test_pg_replicacion import Cluster, tool
from pg_helpers import ROOT

pytestmark = pytest.mark.skipif(not os.getenv("NEXUSTIX_TEST_PG_BIN"), reason="Requiere binarios PostgreSQL")


def test_backup_wal_restore_excludes_later_transaction(tmp_path, monkeypatch):
    import psycopg2
    spec = importlib.util.spec_from_file_location("physical_backup", ROOT / "scripts/physical_backup.py")
    backup_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(backup_module)
    primary, recovered = Cluster(tmp_path, "original"), Cluster(tmp_path, "restored")
    archive = tmp_path / "archive"; archive.mkdir()
    subprocess.run([tool("initdb"), "-D", str(primary.data), "-U", "postgres", "--auth=trust", "--locale=C"], check=True, capture_output=True)
    # Espacios en el repositorio: invocar sh con argumento de ruta entre comillas.
    import shlex
    command = "sh " + shlex.quote(str(ROOT / "postgres/archive-wal.sh")) + " %p %f"
    with (primary.data / "postgresql.conf").open("a") as config:
        config.write("\nwal_level=replica\narchive_mode=on\narchive_command='" + command.replace("'", "''") + "'\n")
    monkeypatch.setenv("NEXUSTIX_WAL_ARCHIVE", str(archive))
    primary.start()
    try:
        conn = psycopg2.connect(primary.url()); conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE ledger (id integer PRIMARY KEY)")
            cursor.execute("INSERT INTO ledger VALUES (1)")
        monkeypatch.setenv("PGHOST", "127.0.0.1"); monkeypatch.setenv("PGPORT", str(primary.port)); monkeypatch.setenv("PGUSER", "postgres")
        source = backup_module.backup(tmp_path / "backup", os.getenv("NEXUSTIX_TEST_PG_BIN"))
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO ledger VALUES (2)")
            cursor.execute("SELECT pg_create_restore_point('before_third')")
            cursor.execute("INSERT INTO ledger VALUES (3)")
            cursor.execute("SELECT pg_walfile_name(pg_current_wal_lsn())"); segment = cursor.fetchone()[0]
            cursor.execute("SELECT pg_switch_wal()")
        deadline = time.monotonic() + 20
        while not (archive / segment).exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert (archive / segment).exists(), "WAL no archivado"
        conn.close()
        backup_module.restore(source, recovered.data, archive, "before_third", os.getenv("NEXUSTIX_TEST_PG_BIN"))
        recovered.start()
        try:
            connection = psycopg2.connect(recovered.url())
            connection.autocommit = True
            with connection.cursor() as cursor:
                # pg_ctl -w también acepta un hot standby listo para lecturas.
                # Esperar la promoción solicitada antes de verificar el estado final.
                deadline = time.monotonic() + 20
                while True:
                    cursor.execute("SELECT pg_is_in_recovery()")
                    if cursor.fetchone()[0] is False:
                        break
                    assert time.monotonic() < deadline, "PITR no finalizó la promoción"
                    time.sleep(0.1)
                cursor.execute("SELECT id FROM ledger ORDER BY id")
                assert cursor.fetchall() == [(1,), (2,)]
                cursor.execute("INSERT INTO ledger VALUES (4)")
                cursor.execute("SELECT id FROM ledger ORDER BY id")
                assert cursor.fetchall() == [(1,), (2,), (4,)]
            connection.close()
        finally:
            recovered.stop()
        with pytest.raises(ValueError):
            backup_module.restore(source, primary.data, archive, "before_third", os.getenv("NEXUSTIX_TEST_PG_BIN"))
    finally:
        primary.stop()

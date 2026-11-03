"""Opt-in Docker test of the restore verifier with wholly synthetic data.

Run with NEXUSTIX_BACKUP_DOCKER_TEST=1. It never inspects or connects to the
main/staging containers. Failed runs deliberately retain their synthetic
fixture and Docker resources for diagnosis.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from scripts import backup_main_databases as backup


ENABLED = os.getenv("NEXUSTIX_BACKUP_DOCKER_TEST") == "1"


def docker(args, *, stdin=None, stdout=None, timeout=1800):
    result = subprocess.run(["docker", *args], stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-1500:])
    return result


@unittest.skipUnless(ENABLED, "Opt-in: requiere Docker; usa solo PostgreSQL sintético aislado")
class IsolatedRestoreIntegrationTest(unittest.TestCase):
    def test_restore_compare_and_migration_rehearsal_use_only_synthetic_copy(self):
        suffix = uuid.uuid4().hex[:10]
        source_network = f"nex-backup-fixture-net-{suffix}"
        source_container = f"nex-backup-fixture-db-{suffix}"
        fixture = Path(tempfile.mkdtemp(prefix="nexustix-backup-fixture-"))
        resources_created = []
        try:
            docker(["network", "create", "--internal", source_network])
            resources_created.append(source_network)
            docker([
                "run", "--detach", "--name", source_container, "--network", source_network,
                "--network-alias", "fixture-db", "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,noexec",
                "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:15-alpine",
            ])
            resources_created.append(source_container)
            backup._wait_for_initialized_postgres(source_container, 90)

            for name in backup.DATABASES:
                docker(["exec", source_container, "createdb", "-U", "postgres", name])

            ddl = {
                "usuarios_db": """
                    CREATE TABLE usuarios(id serial PRIMARY KEY, nombre varchar NOT NULL, email varchar NOT NULL UNIQUE, estado varchar);
                    INSERT INTO usuarios(nombre,email,estado) VALUES ('Synthetic One','one@example.test','activo');
                """,
                "eventos_db": """
                    CREATE TABLE eventos(id serial PRIMARY KEY, nombre varchar NOT NULL, stock integer NOT NULL);
                    CREATE TABLE reservas_stock(operacion_id varchar(64) PRIMARY KEY, evento_id integer NOT NULL, cantidad integer NOT NULL, estado varchar(20) NOT NULL);
                    INSERT INTO eventos(nombre,stock) VALUES ('Synthetic Event', 8);
                    INSERT INTO reservas_stock VALUES ('op-confirmed',1,2,'confirmada'),('op-released',1,3,'liberada');
                """,
                "compras_db": """
                    CREATE TABLE compras(id serial PRIMARY KEY, usuario_id integer NOT NULL, evento_id integer NOT NULL,
                      cantidad integer NOT NULL, estado varchar DEFAULT 'confirmada', instancia varchar,
                      creado_en timestamptz DEFAULT now(), operacion_id varchar(64),
                      CONSTRAINT ck_compras_cantidad_positiva CHECK (cantidad > 0));
                    CREATE UNIQUE INDEX uq_compras_operacion_id ON compras(operacion_id) WHERE operacion_id IS NOT NULL;
                    CREATE TABLE journal_compras(id serial PRIMARY KEY, timestamp timestamptz NOT NULL DEFAULT now(),
                      reloj_t integer NOT NULL, instancia varchar(16) NOT NULL, tipo varchar(40) NOT NULL,
                      mensaje varchar NOT NULL, detalles json NOT NULL DEFAULT '{}'::json);
                    INSERT INTO compras(usuario_id,evento_id,cantidad,estado,instancia,operacion_id)
                      VALUES (1,1,2,'confirmada','fixture','op-confirmed');
                    INSERT INTO journal_compras(reloj_t,instancia,tipo,mensaje) VALUES (1,'fixture','COMPRA','Synthetic journal');
                """,
            }
            manifest = {
                "format_version": backup.FORMAT_VERSION,
                "created_utc": backup.utc_now(),
                "compose_project": "synthetic-fixture",
                "status": "complete",
                "verified": False,
                "databases": {},
            }
            for name in backup.DATABASES:
                docker(["exec", source_container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", name, "-c", ddl[name]])
                snapshot = backup._psql(source_container, name,
                                        "SELECT json_build_object('version', current_setting('server_version'), 'version_num', current_setting('server_version_num'))::text")
                stats = backup._psql(source_container, name, backup.STATS_SQL[name])
                dump_path = fixture / f"{name}.dump"
                with dump_path.open("xb") as archive:
                    docker(["exec", "-i", "-u", "postgres", source_container, "pg_dump", "-U", "postgres", "-d", name,
                            "--format=custom", "--no-owner", "--no-acl"], stdout=archive)
                manifest["databases"][name] = {
                    "container": source_container,
                    "image": "postgres:15-alpine",
                    "before": {"postgres": json.loads(snapshot), "stats": json.loads(stats)},
                    "dump_file": dump_path.name,
                    "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                }
                dump_path.chmod(0o600)
            (fixture / "SHA256SUMS").write_text("".join(
                f"{manifest['databases'][name]['sha256']}  {name}.dump\n" for name in backup.DATABASES
            ), encoding="ascii")
            (fixture / "SHA256SUMS").chmod(0o600)
            backup.write_json_private(fixture / "manifest.json", manifest)

            result = backup.verify_backup(fixture, rehearse_migrations=True)
            self.assertEqual(result["status"], "verified")
            self.assertTrue(result["migrations_rehearsed"])
            self.assertEqual(set(result["databases"]), set(backup.DATABASES))
            self.assertTrue(all(item["counts_match"] and item["after_migration_stats_match"]
                                for item in result["databases"].values()))
        except Exception:
            print(f"Synthetic fixture retained for diagnosis: {fixture}")
            print(f"Synthetic Docker resources retained when created: {resources_created}")
            raise
        else:
            subprocess.run(["docker", "stop", source_container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["docker", "rm", source_container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["docker", "network", "rm", source_network], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
            shutil.rmtree(fixture)


if __name__ == "__main__":
    unittest.main()

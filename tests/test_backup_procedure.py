import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backup_main_databases as backup


class BackupProcedureTests(unittest.TestCase):
    def test_requires_explicit_write_pause_before_creating_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "backup"
            with self.assertRaisesRegex(backup.ProcedureError, "confirm-writes-paused"):
                backup.create_backup(destination, "project", False)
            self.assertFalse(destination.exists())

    def test_stats_comparison_detects_change(self):
        backup.assert_same_stats({"count": 2}, {"count": 2}, "test_db")
        with self.assertRaisesRegex(backup.ProcedureError, "no coinciden"):
            backup.assert_same_stats({"count": 2}, {"count": 3}, "test_db")

    def test_postgres_major_is_derived_from_server_version_num(self):
        self.assertEqual(backup.postgres_major({"postgres": {"version_num": "150013"}}), 15)

    def test_existing_backup_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "backup"
            destination.mkdir()
            sentinel = destination / "keep.txt"
            sentinel.write_text("retained")
            with self.assertRaisesRegex(backup.ProcedureError, "ya existe"):
                backup.create_backup(destination, "nexustix", True, "demo")
            self.assertEqual(sentinel.read_text(), "retained")

    def test_manifest_is_private_and_hash_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "format_version": backup.FORMAT_VERSION,
                "status": "complete",
                "databases": {
                    name: {"dump_file": f"{name}.dump", "sha256": "0" * 64}
                    for name in backup.DATABASES
                },
            }
            for name in backup.DATABASES:
                (root / f"{name}.dump").write_bytes(b"synthetic archive")
            (root / "SHA256SUMS").write_text("\n".join(
                f"{'0' * 64}  {name}.dump" for name in backup.DATABASES
            ) + "\n")
            with self.assertRaisesRegex(backup.ProcedureError, "SHA-256"):
                backup._check_backup_files(root, manifest)

            path = root / "manifest.json"
            backup.write_json_private(path, {"safe": True})
            if os.name == "posix":  # Windows no aplica permisos POSIX
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"safe": True})

    def test_environment_and_project_must_match_before_anything_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "backup"
            with self.assertRaisesRegex(backup.ProcedureError, "Destino rechazado"):
                backup.create_backup(destination, "nexustix", True, "pruebas")
            self.assertFalse(destination.exists())

    @patch.object(backup, "run_checked")
    def test_compose_target_mismatch_is_rejected_before_dump(self, run_checked):
        def fake(args, **_kwargs):
            if args[:2] == ["docker", "ps"]:
                listed = b"abc123" if "com.docker.compose.service=usuarios_db" in " ".join(args) else b""
                return type("Result", (), {"stdout": listed})()
            if "com.docker.compose.project" in " ".join(args):
                return type("Result", (), {"stdout": b"nexustix-auth-staging"})()
            return type("Result", (), {"stdout": b"usuarios_db"})()
        run_checked.side_effect = fake
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "backup"
            with self.assertRaisesRegex(backup.ProcedureError, "Destino rechazado"):
                backup.create_backup(destination, "nexustix", True, "demo")
            self.assertTrue((destination / "manifest.json").exists())
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(any("pg_dump" in " ".join(c.args[0]) for c in run_checked.call_args_list))

    @patch.object(backup, "_psql", return_value="f")
    @patch.object(backup, "_docker_label")
    @patch.object(backup, "run_checked")
    def test_two_writable_nodes_abort_the_backup(self, run_checked, label, _psql):
        # Un contenedor por nodo (compras_db y compras_db_replica) y ambos
        # responden pg_is_in_recovery() = f: dos primarios.
        run_checked.return_value = type("Result", (), {"stdout": b"c1"})()
        services = iter(["compras_db", "compras_db_replica"])
        label.side_effect = lambda container, name: "nexustix" if name.endswith("project") else next(services)
        entorno = backup.entornos.get("demo")
        with self.assertRaisesRegex(backup.ProcedureError, "dos nodos"):
            backup.resolve_primary_container(entorno, "compras_db")

    def test_schema_comparison_detects_missing_constraint_and_row_change(self):
        schema = {"tables": ["compras"], "columns": ["compras.id:integer:NO"], "constraints": ["compras.ck:c"],
                  "foreign_keys": [], "indexes": ["compras.pk"], "alembic_revision": "20261023_02",
                  "row_counts": {"compras": 3, "refresh_sessions": 5}}
        backup.compare_schema(schema, dict(schema, row_counts={"compras": 3, "refresh_sessions": 9}), "compras_db")
        with self.assertRaisesRegex(backup.ProcedureError, "constraints"):
            backup.compare_schema(schema, dict(schema, constraints=[]), "compras_db")
        with self.assertRaisesRegex(backup.ProcedureError, "alembic_revision"):
            backup.compare_schema(schema, dict(schema, alembic_revision="20261022_01"), "compras_db")
        with self.assertRaisesRegex(backup.ProcedureError, "filas"):
            backup.compare_schema(schema, dict(schema, row_counts={"compras": 2, "refresh_sessions": 5}), "compras_db")

    def test_restore_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(backup.ProcedureError, "confirm-empty-target"):
                backup.restore_backup(Path(tmp), "nexustix", "demo", False)

if __name__ == "__main__":
    unittest.main()

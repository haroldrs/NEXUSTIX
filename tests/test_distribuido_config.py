"""La nueva topología nunca reutiliza recursos de demo/staging."""
from pathlib import Path
import importlib.util
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_lab_resource_names_and_ports_do_not_collide():
    lab = yaml.safe_load((ROOT / "docker-compose.distribuido.yml").read_text())
    existing = [yaml.safe_load((ROOT / file).read_text()) for file in ("docker-compose.yml", "docker-compose.staging.yml")]
    assert lab["name"] not in {doc["name"] for doc in existing}
    volumes = {v["name"] for v in lab["volumes"].values()}
    for doc in existing:
        assert not volumes & {v.get("name") for v in doc["volumes"].values()}
    for service in lab["services"].values():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:2808")
        assert "container_name" not in service
    assert len([s for s in lab["services"] if s.endswith("_replica")]) == 3


def test_vm_apps_depend_on_remote_database_urls_not_local_database_containers():
    app = yaml.safe_load((ROOT / "despliegue-vm/compose.app.yml").read_text())
    assert not any("_db" in s for s in app["services"])
    for svc in app["services"].values():
        assert not any("_db" in name for name in svc.get("depends_on", {}))
    standby = yaml.safe_load((ROOT / "despliegue-vm/compose.standby.yml").read_text())
    assert sorted(s["environment"]["NEXUSTIX_PG_UPSTREAM_PORT"] for s in standby["services"].values()) == ["5432", "5433", "5434"]


def test_restore_refuses_existing_directory_before_running_any_command(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("physical_backup", ROOT / "scripts/physical_backup.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        raise AssertionError("No debe ejecutar PostgreSQL contra un destino existente")
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    import pytest
    with pytest.raises(ValueError, match="no exista"):
        module.restore(tmp_path / "source", tmp_path, tmp_path / "archive", "point")

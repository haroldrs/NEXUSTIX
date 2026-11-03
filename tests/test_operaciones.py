"""Salvaguardas de los scripts de operación (db_ha.py, nx.py, pg_docker.py) con Docker simulado.

Verifican el orden del protocolo de promoción (aislar antes de promover) y que
las operaciones destructivas se niegan sin confirmación explícita.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import db_ha  # noqa: E402
import entornos  # noqa: E402
import nx  # noqa: E402
import pg_docker  # noqa: E402

PRUEBAS = entornos.get("pruebas")
DEMO = entornos.get("demo")


def nodo(servicio, running=True, standby=False):
    return pg_docker.Nodo(servicio, f"id-{servicio}", running, standby if running else None)


def test_two_writable_nodes_are_reported_not_resolved(monkeypatch):
    monkeypatch.setattr(pg_docker, "nodos", lambda *_: [nodo("compras_db"), nodo("compras_db_replica")])
    with pytest.raises(pg_docker.DosPrimariosError):
        pg_docker.primario(PRUEBAS, "compras_db")


def test_promotion_fences_the_old_primary_before_promoting(monkeypatch):
    calls = []
    monkeypatch.setattr(pg_docker, "nodos", lambda *_: [nodo("compras_db"), nodo("compras_db_replica", standby=True)])
    monkeypatch.setattr(entornos, "verify_labels", lambda *a: calls.append(("verify", a[2])))
    monkeypatch.setattr(entornos, "inspect", lambda *_: "nexustix-staging/postgres:15")
    monkeypatch.setattr(db_ha, "_docker", lambda *a, **k: calls.append(("docker", a[0], a[-1] if a[0] == "run" else a[1])))
    state = {"promoted": False}

    def psql(container, _db, sql, **_k):
        calls.append(("psql", container, sql.strip().split("(")[0]))
        if "pg_promote" in sql:
            state["promoted"] = True
        if "pg_is_in_recovery" in sql:
            return "f" if state["promoted"] else "t"
        return ""

    monkeypatch.setattr(pg_docker, "psql", psql)
    monkeypatch.setattr(pg_docker, "psql_json", lambda *a, **k: {"pendiente_bytes": 0})
    result = db_ha.promote(PRUEBAS, "compras_db")

    assert result["nuevo_primario"] == "compras_db_replica" and result["aislado"] == "compras_db"
    order = [c[0] + ":" + str(c[1]) for c in calls]
    stop = order.index("docker:stop")
    no_restart = order.index("docker:update")
    fence = next(i for i, c in enumerate(calls) if c[0] == "docker" and c[1] == "run")
    promote = next(i for i, c in enumerate(calls) if c[0] == "psql" and c[2].startswith("SELECT pg_promote"))
    assert calls[0] == ("verify", "compras_db"), "se verifican etiquetas antes de tocar el contenedor"
    assert stop < no_restart < fence < promote, "el primario anterior queda aislado antes de promover"
    assert "NEXUSTIX_FENCED" in calls[fence][2]


def test_promotion_requires_exactly_one_running_standby(monkeypatch):
    monkeypatch.setattr(pg_docker, "nodos", lambda *_: [nodo("compras_db")])
    with pytest.raises(pg_docker.PgError, match="exactamente un standby"):
        db_ha.promote(PRUEBAS, "compras_db")


def test_unreachable_old_primary_needs_operator_confirmation(monkeypatch):
    monkeypatch.setattr(pg_docker, "nodos", lambda *_: [nodo("compras_db_replica", standby=True)])
    monkeypatch.setattr(pg_docker, "psql_json", lambda *a, **k: {})
    monkeypatch.setattr(pg_docker, "psql", lambda *a, **k: pytest.fail("no debe promover sin confirmación"))
    with pytest.raises(pg_docker.PgError, match="primario-aislado-manualmente"):
        db_ha.promote(PRUEBAS, "compras_db")


def test_rejoin_never_rebuilds_the_current_primary(monkeypatch):
    monkeypatch.setattr(pg_docker, "primario", lambda *_: nodo("compras_db_replica"))
    with pytest.raises(pg_docker.PgError, match="primario actual"):
        db_ha.rejoin(PRUEBAS, "compras_db", "compras_db_replica", Path("/tmp/x"))
    with pytest.raises(pg_docker.PgError, match="no es un nodo"):
        db_ha.rejoin(PRUEBAS, "compras_db", "eventos_db", Path("/tmp/x"))


def test_disable_requires_original_primaries(monkeypatch):
    monkeypatch.setattr(pg_docker, "primario", lambda _e, base: nodo(f"{base}_replica" if base == "eventos_db" else base))
    monkeypatch.setattr(db_ha, "_compose", lambda *a, **k: pytest.fail("no debe tocar contenedores"))
    with pytest.raises(pg_docker.PgError, match="switchback eventos_db"):
        db_ha.disable(PRUEBAS)


def test_replication_password_with_quotes_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "replication-password").write_text("x'; DROP ROLE postgres; --")
    monkeypatch.setenv(PRUEBAS.secrets_env, str(tmp_path))
    with pytest.raises(pg_docker.PgError, match="no admitidos"):
        db_ha._replication_password(PRUEBAS)


def test_destructive_commands_require_explicit_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(nx, "compose", lambda *a, **k: pytest.fail("no debe ejecutar docker compose"))
    assert nx.cmd_reset_datos(PRUEBAS, SimpleNamespace(confirmar="", respaldo_verificado=None, incluir_usuarios=False)) == 1
    assert nx.cmd_reset_datos(DEMO, SimpleNamespace(confirmar="nexustix-auth-staging", respaldo_verificado=None,
                                                    incluir_usuarios=False)) == 1
    assert nx.cmd_reset_datos(DEMO, SimpleNamespace(confirmar="nexustix", respaldo_verificado=None,
                                                    incluir_usuarios=False)) == 1, "demo exige respaldo verificado"
    assert nx.cmd_down(DEMO, SimpleNamespace(confirmar="")) == 1
    assert nx.cmd_verificar(DEMO, SimpleNamespace(compra=True, permitir_compra_en_demo=False)) == 1
    assert "exige" in capsys.readouterr().err


def test_demo_reset_accepts_only_a_verified_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(nx, "_exec_sql", lambda *a: "f")
    calls = []
    monkeypatch.setattr(nx, "compose", lambda *a, **k: calls.append(a[1:]) or SimpleNamespace(returncode=1))
    (tmp_path / "verification.json").write_text('{"status": "failed"}')
    args = SimpleNamespace(confirmar="nexustix", respaldo_verificado=str(tmp_path), incluir_usuarios=False)
    assert nx.cmd_reset_datos(DEMO, args) == 1 and calls == []
    (tmp_path / "verification.json").write_text('{"status": "verified"}')
    assert nx.cmd_reset_datos(DEMO, args) == 1  # el stop simulado falla: se detiene sin tocar datos
    assert calls == [("stop", "compras1", "compras2", "eventos")]


def test_container_lookup_filters_by_exact_project_labels(monkeypatch):
    seen = {}

    def run(args, **_kwargs):
        seen["args"] = args
        return SimpleNamespace(returncode=0, stdout="abc\n", stderr="")

    monkeypatch.setattr(entornos, "run", run)
    assert entornos.containers(PRUEBAS, "compras1") == ["abc"]
    joined = " ".join(seen["args"])
    assert "label=com.docker.compose.project=nexustix-auth-staging" in joined
    assert "label=com.docker.compose.service=compras1" in joined
    assert "label=com.docker.compose.oneoff=False" in joined

"""R21 · Replicación PostgreSQL, promoción controlada con fencing, reincorporación y retorno.

Requiere el overlay de replicación en el entorno de PRUEBAS:
    python3 scripts/db_ha.py pruebas enable
La prueba deja el entorno en su estado normal (compras_db primario,
compras_db_replica standby). Si falla a mitad, 'db_ha.py pruebas status'
indica el estado y 'rejoin'/'switchback' lo restablecen.
"""
import json
import subprocess
import sys
import time
import uuid

import pytest

import nx_pruebas as nx
from nx_pruebas import pg_docker


def db_ha(*args, timeout=1800):
    result = subprocess.run([sys.executable, str(nx.ROOT / "scripts" / "db_ha.py"), "pruebas", *args],
                            cwd=nx.ROOT, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return result.stdout


def status():
    return json.loads(db_ha("status"))


def node(service):
    return next(n for n in pg_docker.nodos(nx.ENT, "compras_db") if n.servicio == service)


def on_node(service, statement):
    return pg_docker.psql(node(service).container, "compras_db", statement)


@pytest.mark.skipif(not nx.ENABLED or not nx.ha_active(), reason="Requiere 'db_ha.py pruebas enable'")
def test_r21_replicacion_promocion_fencing_reincorporacion_y_retorno(buyer):
    """Esperado: (1) las tres bases REPLICADO; (2) una compra aparece en el standby; (3) tras
    'promote' el nodo original queda detenido, sin reinicio automático y su arranque es rechazado
    (exit 64); (4) las APIs escriben en el nuevo primario sin reconfiguración y la compra previa
    sigue allí; (5) 'rejoin' lo convierte en standby; (6) 'switchback' devuelve los roles; (7) en
    ningún momento hay dos primarios. Criterio: todas las aserciones y compras coherentes."""
    assert {db: info["veredicto"] for db, info in status().items()} == {
        "usuarios_db": "REPLICADO", "eventos_db": "REPLICADO", "compras_db": "REPLICADO"}
    event = nx.create_event(10)
    key_before = f"r21-antes-{uuid.uuid4().hex}"
    assert nx.purchase(buyer["token"], event, key_before).status_code == 200
    op_before = nx.claim(key_before)["op"]
    nx.wait_until(lambda: on_node("compras_db_replica", f"SELECT count(*) FROM compras WHERE operacion_id = '{op_before}'") == "1",
                  30, "la compra no llegó al standby")

    started = time.monotonic()
    db_ha("promote", "compras_db")
    old = node("compras_db")
    assert not old.running
    assert nx.inspect(old.container, "{{.HostConfig.RestartPolicy.Name}}") == "no"
    subprocess.run(["docker", "start", old.container], capture_output=True, timeout=60)
    nx.wait_until(lambda: nx.inspect(old.container, "{{.State.Running}}") == "false", 30, "el nodo aislado arrancó")
    assert nx.inspect(old.container, "{{.State.ExitCode}}") == "64", "el entrypoint rechaza el nodo aislado"
    assert pg_docker.primario(nx.ENT, "compras_db").servicio == "compras_db_replica"

    key_after = f"r21-despues-{uuid.uuid4().hex}"
    response = nx.purchase_until_done(buyer["token"], event, key_after, timeout=120)
    recovery_seconds = time.monotonic() - started
    assert response.status_code == 200
    assert on_node("compras_db_replica", f"SELECT count(*) FROM compras WHERE operacion_id = '{op_before}'") == "1"
    print(f"\nTiempo desde 'promote' hasta la primera compra correcta: {recovery_seconds:.1f} s")

    db_ha("rejoin", "compras_db", "compras_db")
    assert node("compras_db").en_recuperacion is True
    assert status()["compras_db"]["veredicto"] == "REPLICADO"

    db_ha("switchback", "compras_db")
    assert pg_docker.primario(nx.ENT, "compras_db").servicio == "compras_db"
    assert node("compras_db_replica").en_recuperacion is True
    key_final = f"r21-final-{uuid.uuid4().hex}"
    assert nx.purchase_until_done(buyer["token"], event, key_final, timeout=120).status_code == 200
    state = nx.event_state(event)
    assert len(state["compras"]) == 3 and state["stock"] == 7
    assert all(r["estado"] == "confirmada" for r in state["reservas"])
    assert status()["compras_db"]["veredicto"] == "REPLICADO"

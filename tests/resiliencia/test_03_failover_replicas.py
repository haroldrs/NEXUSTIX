"""R06-R08 · Caída abrupta de Compras1, de Compras2 y de ambas; recuperación.

docker kill (SIGKILL) del contenedor exacto: Docker NO lo reinicia (parada
manual), lo que permite observar el failover del balanceador. Limpieza: se
arranca el mismo contenedor y se espera healthy.
"""
import uuid

import pytest
import requests

import nx_pruebas as nx


@pytest.mark.parametrize("victim,survivor", [("compras1", "compras2"), ("compras2", "compras1")])
def test_r06_r07_caida_de_una_replica_failover_y_reincorporacion(buyer, victim, survivor):
    """Objetivo: con una réplica muerta las compras siguen por la otra; al volver, ambas atienden.
    Esperado: contenedor víctima Running=false/ExitCode=137; 6 compras por el LB con 200 atendidas
    por la superviviente; tras arrancarla, el LB vuelve a repartir entre las dos.
    Criterio: 6 compras = 6 filas y stock -6; las dos instancias aparecen en atendido_por."""
    event = nx.create_event(20)
    cid = nx.kill(victim)
    try:
        assert nx.inspect(cid, "{{.State.ExitCode}}") == "137"
        served = set()
        for i in range(6):
            key = f"r06-{victim}-{i}-{uuid.uuid4().hex}"
            response = nx.purchase(buyer["token"], event, key)
            assert response.status_code == 200, response.text
            served.add(response.json()["atendido_por"])
        assert served == {f"compras (instancia {survivor[-1]})"}, served
        state = nx.event_state(event)
        assert len(state["compras"]) == 6 and state["stock"] == 14
    finally:
        nx.start(cid, victim)
    nx.wait_until(lambda: requests.get(f"{nx.REPLICA_URL[victim]}/health", timeout=3).status_code == 200,
                  60, f"{victim} no volvió")
    served = set()

    def both_serve():
        response = nx.purchase(buyer["token"], event, f"r06-back-{uuid.uuid4().hex}")
        assert response.status_code == 200, response.text
        served.add(response.json()["atendido_por"])
        return len(served) == 2
    nx.wait_until(both_serve, 60, "el balanceador no volvió a usar la réplica reincorporada", interval=1)


def test_r08_ambas_replicas_caidas_no_produce_efectos_y_se_recupera(buyer):
    """Objetivo: sin réplicas el sistema falla de forma limpia y se recupera al volver.
    Esperado: el LB responde 502 (o error de conexión); la clave no existe en BD y el stock no
    cambia; tras arrancar ambas, el reintento con la MISMA clave crea exactamente una compra."""
    event = nx.create_event(3)
    key = f"r08-{uuid.uuid4().hex}"
    killed = [(nx.kill(service), service) for service in ("compras1", "compras2")]
    try:
        try:
            response = nx.purchase(buyer["token"], event, key, timeout=40)
            assert response.status_code in {502, 503, 504}, response.text
        except requests.RequestException:
            pass
        assert nx.claim(key) is None and nx.event_state(event)["stock"] == 3
        assert requests.get(f"{nx.LB}/lb-health", timeout=5).status_code == 200, "el balanceador sigue vivo"
    finally:
        for cid, service in killed:
            nx.start(cid, service)
    response = nx.purchase_until_done(buyer["token"], event, key)
    assert response.status_code == 200
    nx.assert_single_consistent_purchase(event, key, 3)

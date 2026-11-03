"""R09-R12 · Terminación abrupta en cada punto de la saga y reintento por la otra réplica.

Mecanismo determinista: compras1 se recrea con NEXUSTIX_TEST_CRASH_HOOKS=1
(solo en pruebas). La petición se envía directa a compras1 con
X-NEXUSTIX-Test-Hold-At=<checkpoint>: el proceso escribe un marcador en su log
y se detiene ahí; la prueba confirma el marcador y hace docker kill del
contenedor exacto. Después reintenta la MISMA clave por el balanceador (lo
atiende compras2) y comprueba el resultado en ambas bases.
Limpieza: compras1 se recrea con los ganchos apagados aunque la prueba falle.
"""
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests

import nx_pruebas as nx

CHECKPOINTS = {
    # checkpoint: (estado de la clave antes del reintento, ¿hay reserva antes del reintento?)
    "before_claim": (None, False),
    "after_claim": ("processing", False),
    "after_reservation": ("processing", True),
    "after_purchase_commit": ("completed", True),
    "after_final_response_commit": ("completed", True),
}


@pytest.fixture
def hooks_on_compras1():
    nx.set_test_hooks("compras1", True)
    try:
        yield
    finally:
        nx.set_test_hooks("compras1", False)


@pytest.mark.parametrize("checkpoint", list(CHECKPOINTS))
def test_r09_r11_caida_abrupta_en_cada_checkpoint_y_reintento(buyer, hooks_on_compras1, checkpoint):
    """Objetivo: una caída en cualquier punto no duplica compras ni descuadra el stock.
    Esperado antes del reintento: estado durable del checkpoint (clave/reserva); el cliente no
    recibió respuesta. Esperado tras reintentar con la misma clave por el LB: HTTP 200 de compras2,
    exactamente 1 compra, reserva confirmada (tras recuperación si quedó pendiente) y stock -1."""
    event = nx.create_event(4)
    key = f"r09-{checkpoint}-{uuid.uuid4().hex}"
    cid = nx.container("compras1")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    outcome = {}

    def client():
        try:
            outcome["response"] = nx.purchase(buyer["token"], event, key, url=nx.R1,
                                              headers={"X-NEXUSTIX-Test-Hold-At": checkpoint}, timeout=90)
        except requests.RequestException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=client)
    thread.start()
    nx.wait_until(lambda: nx.logs_contain(cid, f"NEXUSTIX_TEST_HOLD checkpoint={checkpoint}", since), 30,
                  f"compras1 no alcanzó {checkpoint}")
    nx.kill("compras1")
    thread.join(30)
    assert "error" in outcome, f"el cliente no debía recibir respuesta: {outcome}"
    assert nx.inspect(cid, "{{.State.ExitCode}}") == "137"

    expected_claim, expected_reservation = CHECKPOINTS[checkpoint]
    record = nx.claim(key)
    assert (record or {}).get("state") == expected_claim, record
    state = nx.event_state(event)
    assert bool(state["reservas"]) == expected_reservation, state
    assert len(state["compras"]) == (1 if expected_claim == "completed" else 0)

    response = nx.purchase_until_done(buyer["token"], event, key)
    assert response.status_code == 200, response.text
    assert response.json()["atendido_por"].endswith("2)") or response.headers.get("Idempotency-Replayed") == "true"
    if checkpoint == "after_purchase_commit":
        # La compra quedó persistida antes de confirmar en Eventos: el replay lo dice y la
        # recuperación (admin o automática) confirma la reserva sin crear otra compra.
        assert response.json()["confirmacion_reserva"] == "pendiente"
        admin = nx.admin_token()
        result = nx.recover(admin)
        assert result["contadores"]["reservas_confirmadas"] >= 1
        assert nx.claim(key)["confirmacion"] == "confirmada", "el replay quedó reparado"
        # Repetir la recuperación no vuelve a actuar sobre esta operación.
        again = nx.recover(admin)
        assert all(d.get("operacion_id") != record["op"] for d in again["diagnosticos"])
    nx.assert_single_consistent_purchase(event, key, 4)


def test_r12_caida_del_proceso_con_reinicio_automatico_de_docker(buyer, hooks_on_compras1):
    """Objetivo: distinguir reinicio automático de contenedor de failover.
    Preparación: X-NEXUSTIX-Test-Crash-At=after_reservation directo a compras1 (os._exit(86)).
    Esperado: el MISMO contenedor reinicia solo (RestartCount +1, política unless-stopped) y
    vuelve a healthy; el reintento con la misma clave completa una única compra."""
    event = nx.create_event(4)
    key = f"r12-{uuid.uuid4().hex}"
    cid = nx.container("compras1")
    restarts = int(nx.inspect(cid, "{{.RestartCount}}"))
    assert nx.inspect(cid, "{{.HostConfig.RestartPolicy.Name}}") == "unless-stopped"
    with pytest.raises(requests.RequestException):
        nx.purchase(buyer["token"], event, key, url=nx.R1, headers={"X-NEXUSTIX-Test-Crash-At": "after_reservation"})
    nx.wait_until(lambda: int(nx.inspect(cid, "{{.RestartCount}}")) == restarts + 1, 60, "Docker no reinició compras1")
    nx.wait_container_healthy("compras1")
    assert nx.container("compras1") == cid, "es el mismo contenedor, no uno recreado"
    response = nx.purchase_until_done(buyer["token"], event, key)
    assert response.status_code == 200
    nx.assert_single_consistent_purchase(event, key, 4)


def test_r12b_failover_transparente_del_balanceador_durante_una_caida(buyer, hooks_on_compras1):
    """Objetivo: una réplica que muere a mitad de POST /compras es transparente para el cliente.
    Preparación: cabecera de caída after_reservation enviada por el LB (solo compras1 la obedece).
    Esperado: alguna petición muestra dos upstreams en X-Nexustix-Upstream (Nginx reintentó la
    misma petición en compras2) y aun así devuelve 200 con una única compra para esa clave."""
    event = nx.create_event(10)
    for attempt in range(6):
        key = f"r12b-{attempt}-{uuid.uuid4().hex}"
        response = nx.purchase(buyer["token"], event, key, headers={"X-NEXUSTIX-Test-Crash-At": "after_reservation"})
        assert response.status_code == 200, response.text
        if "," in response.headers.get("X-Nexustix-Upstream", ""):
            record = nx.claim(key)
            state = nx.event_state(event)
            assert len([c for c in state["compras"] if c["op"] == record["op"]]) == 1
            assert response.json()["atendido_por"] == "compras (instancia 2)"
            nx.wait_container_healthy("compras1")
            return
        nx.wait_container_healthy("compras1")
        time.sleep(11)  # fail_timeout del upstream
    pytest.fail("Nginx nunca enrutó a compras1 en 6 intentos")

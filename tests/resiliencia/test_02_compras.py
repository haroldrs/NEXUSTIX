"""R03-R05 · Flujo normal, idempotencia entre réplicas y compras concurrentes.

Datos: eventos sintéticos (id aleatorio >= 10 000 000, stock_inicial propio) y
compradores qa-resiliencia-*@example.com. Se conservan como evidencia.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor

import nx_pruebas as nx


def test_r03_flujo_normal_de_compra_hasta_postgresql(buyer):
    """Objetivo: frontend→LB→réplica→Eventos→PostgreSQL produce una compra coherente.
    Esperado: HTTP 200, confirmacion_reserva=confirmada; en BD 1 compra, clave completed,
    reserva confirmada y stock -1; aparece en /compras/mias."""
    event = nx.create_event(5)
    key = f"r03-{uuid.uuid4().hex}"
    response = nx.purchase(buyer["token"], event, key)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confirmacion_reserva"] == "confirmada"
    assert response.headers.get("X-Nexustix-Upstream")
    nx.assert_single_consistent_purchase(event, key, 5)
    mine = nx.requests.get(f"{nx.LB}/compras/mias", headers={"Authorization": f"Bearer {buyer['token']}"}, timeout=10)
    assert any(item["id"] == body["id"] for item in mine.json())


def test_r04_misma_clave_en_ambas_replicas_y_en_paralelo_crea_una_compra(buyer):
    """Objetivo: la idempotencia es compartida por las réplicas (PostgreSQL común).
    Preparación: misma clave enviada a compras1, luego a compras2 y 8 veces en paralelo alternando.
    Esperado: siempre la misma compra; compras2 responde Idempotency-Replayed; misma clave con
    otro cuerpo -> 409. Criterio: 1 compra, 1 reserva confirmada, stock -1."""
    event = nx.create_event(10)
    key = f"r04-{uuid.uuid4().hex}"
    first = nx.purchase(buyer["token"], event, key, url=nx.R1)
    assert first.status_code == 200, first.text
    replay = nx.purchase(buyer["token"], event, key, url=nx.R2)
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    assert replay.headers.get("Idempotency-Replayed") == "true"
    other = nx.purchase(buyer["token"], event, key, url=nx.R2, quantity=2)
    assert other.status_code == 409

    key2 = f"r04-par-{uuid.uuid4().hex}"
    urls = [nx.R1, nx.R2] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda url: nx.purchase(buyer["token"], event, key2, url=url), urls))
    assert {r.status_code for r in results} == {200}, [r.text for r in results]
    assert len({r.json()["id"] for r in results}) == 1
    state = nx.event_state(event)
    assert len(state["compras"]) == 2 and state["stock"] == 8, state


def test_r05_ultima_entrada_concurrente_se_vende_una_vez(buyer):
    """Objetivo: compras concurrentes de la última entrada no sobrevenden.
    Preparación: evento con stock 1; 8 compradores distintos compran a la vez por el LB.
    Esperado: exactamente un 200 y siete 409; stock 0 (nunca negativo); una compra y una reserva."""
    event = nx.create_event(1)
    buyers = [nx.register_buyer() for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda b: nx.purchase(b["token"], event, f"r05-{uuid.uuid4().hex}"), buyers))
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200] + [409] * 7, [(r.status_code, r.text[:120]) for r in results]
    state = nx.event_state(event)
    assert state["stock"] == 0 and len(state["compras"]) == 1
    assert [r["estado"] for r in state["reservas"]] == ["confirmada"]

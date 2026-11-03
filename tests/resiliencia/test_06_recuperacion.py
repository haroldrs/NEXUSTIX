"""R16-R17 · Recuperación de operaciones incompletas y repetición de la recuperación.

Las operaciones incompletas se fabrican con una caída real (gancho de pausa +
docker kill) sobre compras1; nunca editando tablas salvo para envejecer la
clave de un caso sintético cuando la prueba necesita superar la ventana stale.
"""
import threading
import uuid
from datetime import datetime, timezone

import pytest
import requests

import nx_pruebas as nx


@pytest.fixture
def hooks_on_compras1():
    nx.set_test_hooks("compras1", True)
    try:
        yield
    finally:
        nx.set_test_hooks("compras1", False)


def crash_at(buyer, event, key, checkpoint):
    cid = nx.container("compras1")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    thread = threading.Thread(target=lambda: _swallow(lambda: nx.purchase(
        buyer["token"], event, key, url=nx.R1, headers={"X-NEXUSTIX-Test-Hold-At": checkpoint}, timeout=90)))
    thread.start()
    nx.wait_until(lambda: nx.logs_contain(cid, f"NEXUSTIX_TEST_HOLD checkpoint={checkpoint}", since), 30, "sin marcador")
    nx.kill("compras1")
    thread.join(30)
    return cid


def _swallow(fn):
    try:
        fn()
    except requests.RequestException:
        pass


def test_r16_reserva_huerfana_se_compensa_una_sola_vez_y_la_recuperacion_es_repetible(buyer, admin, hooks_on_compras1):
    """Objetivo: una caída tras reservar y sin reintento del cliente deja una reserva huérfana
    que la recuperación administrativa compensa exactamente una vez.
    Preparación: caída en after_reservation; la clave se envejece más allá de la ventana stale.
    Esperado: 1.ª recuperación libera 1 reserva (stock vuelve al inicial) y fija la respuesta 409
    'compensada'; 2.ª recuperación no repite nada; el reintento del cliente recibe ese 409."""
    event = nx.create_event(2)
    key = f"r16-{uuid.uuid4().hex}"
    cid = crash_at(buyer, event, key, "after_reservation")
    nx.start(cid, "compras1")
    record = nx.claim(key)
    assert record["state"] == "processing" and nx.event_state(event)["stock"] == 1
    nx.sql("compras_db", f"UPDATE purchase_idempotency SET created_at = now() - interval '1 hour' "
                         f"WHERE idempotency_key = '{key}'")
    first = nx.recover(admin)
    assert any(d.get("operacion_id") == record["op"] for d in first["diagnosticos"]) or first["contadores"]["reservas_liberadas"] >= 1
    state = nx.event_state(event)
    assert state["stock"] == 2 and [r["estado"] for r in state["reservas"]] == ["liberada"] and state["compras"] == []
    second = nx.recover(admin)
    assert all(d.get("operacion_id") != record["op"] for d in second["diagnosticos"])
    assert nx.event_state(event)["stock"] == 2, "la compensación no se aplicó dos veces"
    retry = nx.purchase(buyer["token"], event, key)
    assert retry.status_code == 409 and retry.headers.get("Idempotency-Replayed") == "true"


def test_r17_recuperacion_automatica_confirma_compras_persistidas(buyer, hooks_on_compras1):
    """Objetivo: el proceso automático (una réplica a la vez) confirma reservas de compras ya
    persistidas sin intervención. Requiere NEXUSTIX_STAGING_RECOVERY_INTERVAL_SECONDS>0 al levantar.
    Esperado: tras caer en after_purchase_commit, en < intervalo+20 s la reserva pasa a confirmada
    y el replay idempotente a 'confirmada', con una sola compra."""
    status = requests.get(f"{nx.R2}/health", timeout=5).json()["recuperacion_automatica"]
    if not status["habilitada"]:
        pytest.skip("Levante pruebas con NEXUSTIX_STAGING_RECOVERY_INTERVAL_SECONDS=10 para esta prueba")
    event = nx.create_event(2)
    key = f"r17-{uuid.uuid4().hex}"
    cid = crash_at(buyer, event, key, "after_purchase_commit")
    try:
        nx.wait_until(lambda: nx.claim(key)["confirmacion"] == "confirmada", status["intervalo_s"] + 20,
                      "la recuperación automática no confirmó la compra")
    finally:
        nx.start(cid, "compras1")
    nx.assert_single_consistent_purchase(event, key, 2)

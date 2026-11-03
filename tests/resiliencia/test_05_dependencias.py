"""R13-R15 · Indisponibilidad temporal de Usuarios y de Eventos.

Limpieza: cada prueba vuelve a arrancar (o reanudar) el servicio detenido y
espera a que esté healthy, también si la prueba falla.
"""
import time
import uuid

import pytest
import requests

import nx_pruebas as nx


def test_r13_usuarios_caido_no_impide_comprar_con_un_token_valido(buyer):
    """Objetivo: Compras valida el JWT localmente con la clave pública; no depende de Usuarios.
    Esperado: con Usuarios detenido, login falla (sin conexión) pero la compra con el token ya
    emitido devuelve 200; al volver Usuarios, el login funciona. Criterio: 1 compra coherente."""
    event = nx.create_event(3)
    key = f"r13-{uuid.uuid4().hex}"
    nx.compose("stop", "usuarios")
    try:
        with pytest.raises(requests.RequestException):
            requests.post(f"{nx.USERS}/auth/login", json={"email": buyer["email"], "password": "x" * 12}, timeout=5)
        response = nx.purchase(buyer["token"], event, key)
        assert response.status_code == 200, response.text
    finally:
        nx.compose("start", "usuarios")
        nx.wait_container_healthy("usuarios")
    nx.assert_single_consistent_purchase(event, key, 3)
    assert requests.post(f"{nx.USERS}/auth/register", json={"nombre": "QA", "email": f"qa-r13-{uuid.uuid4().hex[:8]}@example.com",
                                                          "password": "una-clave-de-prueba-larga"}, timeout=10).status_code == 201


def test_r14_eventos_detenido_respuesta_ambigua_sin_efectos_y_reintento(buyer):
    """Objetivo: sin Eventos la compra falla de forma segura y es reanudable.
    Esperado: HTTP 503 con recuperacion_requerida y operacion_id; en BD la clave queda
    'processing', sin compra ni reserva, stock intacto. Tras arrancar Eventos, el reintento con la
    misma clave crea exactamente una compra con esa misma operacion_id."""
    event = nx.create_event(3)
    key = f"r14-{uuid.uuid4().hex}"
    nx.compose("stop", "eventos")
    try:
        response = nx.purchase(buyer["token"], event, key, url=nx.R1)
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert detail["recuperacion_requerida"] is True and detail["operacion_id"]
    finally:
        nx.compose("start", "eventos")
        nx.wait_container_healthy("eventos")
    record = nx.claim(key)
    assert record["state"] == "processing" and record["op"] == detail["operacion_id"]
    state = nx.event_state(event)
    assert state["stock"] == 3 and state["reservas"] == [] and state["compras"] == []
    retry = nx.purchase_until_done(buyer["token"], event, key)
    assert retry.status_code == 200 and retry.json()["operacion_id"] == detail["operacion_id"]
    nx.assert_single_consistent_purchase(event, key, 3)


def test_r15_eventos_colgado_respuesta_perdida_no_duplica(buyer):
    """Objetivo: Eventos congelado (docker pause) agota timeouts acotados sin bloquear el sistema.
    Esperado: la réplica responde 503 en < 30 s; al reanudar Eventos puede procesar la reserva
    que tenía en cola (respuesta perdida). El reintento con la misma clave reutiliza esa reserva:
    1 compra, reserva confirmada, stock -1 exacto."""
    event = nx.create_event(3)
    key = f"r15-{uuid.uuid4().hex}"
    cid = nx.container("eventos")
    nx.docker("pause", cid)
    try:
        started = time.monotonic()
        response = nx.purchase(buyer["token"], event, key, url=nx.R2, timeout=60)
        elapsed = time.monotonic() - started
        assert response.status_code == 503, response.text
        assert elapsed < 30, f"la espera no está acotada ({elapsed:.1f} s)"
    finally:
        nx.docker("unpause", cid)
    nx.wait_container_healthy("eventos")
    retry = nx.purchase_until_done(buyer["token"], event, key)
    assert retry.status_code == 200
    nx.assert_single_consistent_purchase(event, key, 3)

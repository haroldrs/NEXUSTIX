"""R18-R19 · Reinicio de PostgreSQL conservando datos y reconexión de las APIs.

Se reinicia el contenedor exacto de la base del entorno de pruebas (docker
restart y docker stop/start); su volumen nexustix_auth_staging_* persiste.
"""
import uuid

import pytest
import requests

import nx_pruebas as nx


def counts():
    return {
        "compras": int(nx.sql("compras_db", "SELECT count(*) FROM compras")),
        "claves": int(nx.sql("compras_db", "SELECT count(*) FROM purchase_idempotency")),
        "reservas": int(nx.sql("eventos_db", "SELECT count(*) FROM reservas_stock")),
        "usuarios": int(nx.sql("usuarios_db", "SELECT count(*) FROM usuarios")),
    }


@pytest.mark.parametrize("database,mode", [("compras_db", "restart"), ("eventos_db", "stop-start"), ("usuarios_db", "restart")])
def test_r18_reinicio_de_postgresql_conserva_datos_y_las_apis_se_reconectan(buyer, database, mode):
    """Objetivo: un reinicio de PostgreSQL no pierde datos y las APIs vuelven solas.
    Esperado: los conteos previos se conservan; tras quedar healthy, una compra con reintentos
    sobre la MISMA clave termina en 200 sin intervención (pool_pre_ping) y es única; Usuarios
    vuelve a aceptar login. Criterio: conteos posteriores >= previos y compra coherente."""
    if nx.ha_active():
        pytest.skip("Con replicación activa use test_09 (el reinicio de un nodo es parte del failover)")
    event = nx.create_event(3)
    before = counts()
    cid = nx.container(database)
    if mode == "restart":
        nx.docker("restart", cid, timeout=120)
    else:
        nx.docker("stop", cid, timeout=120)
        nx.docker("start", cid, timeout=120)
    nx.wait_container_healthy(database, timeout=120)
    assert nx.container(database) == cid, "mismo contenedor y mismo volumen"
    key = f"r18-{database}-{uuid.uuid4().hex}"
    response = nx.purchase_until_done(buyer["token"], event, key, timeout=120)
    assert response.status_code == 200
    nx.assert_single_consistent_purchase(event, key, 3)
    after = counts()
    assert all(after[k] >= before[k] for k in before), (before, after)
    nx.wait_until(lambda: requests.post(f"{nx.USERS}/auth/register", json={
        "nombre": "QA", "email": f"qa-r18-{uuid.uuid4().hex[:8]}@example.com", "password": "una-clave-de-prueba-larga"},
        timeout=5).status_code == 201, 60, "Usuarios no se reconectó")
    for service in ("usuarios", "eventos", "compras1", "compras2"):
        nx.wait_container_healthy(service, timeout=120)

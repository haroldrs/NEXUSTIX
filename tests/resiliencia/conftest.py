"""Fixtures de la suite de resiliencia (entorno de pruebas aislado, Docker real).

Habilitar con NEXUSTIX_STAGING_HTTP=1 tras levantar el entorno de pruebas:
    python3 scripts/nx.py pruebas up
Al iniciar la sesión se toma la "huella" de solo lectura del entorno de
demostración; la última prueba comprueba que no cambió.
"""
import pytest

import nx_pruebas as nx


def pytest_collection_modifyitems(config, items):
    if nx.ENABLED:
        return
    skip = pytest.mark.skip(reason="Opt-in: NEXUSTIX_STAGING_HTTP=1 con el entorno de pruebas levantado")
    for item in items:
        if "resiliencia" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def demo_before():
    return nx.demo_fingerprint()


@pytest.fixture(scope="session")
def admin():
    nx.all_healthy()
    return nx.admin_token()


@pytest.fixture
def buyer():
    nx.all_healthy()
    return nx.register_buyer()


@pytest.fixture(autouse=True)
def _record_demo_fingerprint(demo_before):
    # Fuerza a tomar la huella de demo antes de la primera prueba.
    yield

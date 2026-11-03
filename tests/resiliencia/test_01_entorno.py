"""R01-R02 · Inicio limpio del entorno aislado y healthchecks.

Preparación: python3 scripts/nx.py pruebas secretos && python3 scripts/nx.py pruebas up
Limpieza: ninguna (solo lectura).
"""
import requests

import nx_pruebas as nx


def test_r01_entorno_aislado_con_proyecto_red_volumenes_y_puertos_propios():
    """Objetivo: el entorno de pruebas es un proyecto Compose propio y aislado.
    Esperado: 9 servicios base del proyecto nexustix-auth-staging, red y volúmenes
    nexustix-auth-staging*/nexustix_auth_staging_*, puertos solo en 127.0.0.1:18080-18085.
    Criterio: todas las comprobaciones de etiquetas y bindings coinciden."""
    for service in entornos_base():
        cid = nx.container(service)
        assert nx.inspect(cid, '{{ index .Config.Labels "com.docker.compose.project" }}') == "nexustix-auth-staging"
        networks = nx.inspect(cid, "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}").split()
        assert networks and all(n.startswith("nexustix-auth-staging") for n in networks), networks
        mounts = nx.inspect(cid, '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}').split()
        assert all(m.startswith("nexustix_auth_staging_") for m in mounts), mounts
        bindings = nx.inspect(cid, "{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostIp}}:{{.HostPort}} {{end}}{{end}}").split()
        assert all(b.startswith("127.0.0.1:18") for b in bindings), bindings


def test_r02_healthchecks_reflejan_salud_funcional():
    """Objetivo: el estado 'healthy' de Docker implica API respondiendo y base conectada.
    Esperado: los 9 contenedores healthy; /health de APIs con db=connected; LB y frontend 200.
    Criterio: Docker Health y HTTP coinciden para todos los servicios."""
    for service in entornos_base():
        nx.wait_container_healthy(service, timeout=180)
    for url in (nx.USERS, nx.EVENTS, nx.R1, nx.R2):
        body = requests.get(f"{url}/health", timeout=8).json()
        assert body["status"] == "ok" and body["db"] == "connected", (url, body)
    assert requests.get(f"{nx.LB}/lb-health", timeout=8).status_code == 200
    assert requests.get(f"{nx.LB}/health", timeout=8).json()["servicio"] == "compras"
    assert requests.get(f"{nx.FRONTEND}/health", timeout=8).status_code == 200
    config = requests.get(f"{nx.FRONTEND}/config.js", timeout=8).text
    assert "localhost:18085" in config, "el frontend de pruebas usa las APIs de pruebas"


def test_r02b_healthcheck_detecta_base_caida_aunque_el_proceso_viva():
    """Objetivo: distinguir 'contenedor en ejecución' de 'servicio disponible'.
    Preparación: se detiene eventos_db (solo pruebas). Esperado: el proceso Eventos sigue vivo
    pero /health responde 503 db=unavailable. Limpieza: se reinicia eventos_db y se espera healthy."""
    db = nx.container("eventos_db")
    nx.docker("stop", db)
    try:
        def unavailable():
            response = requests.get(f"{nx.EVENTS}/health", timeout=5)
            return response.status_code == 503 and response.json().get("db") == "unavailable"
        nx.wait_until(unavailable, 30, "Eventos no reportó la base como no disponible")
        assert nx.inspect(nx.container("eventos"), "{{.State.Running}}") == "true"
    finally:
        nx.start(db, "eventos_db")
        nx.wait_container_healthy("eventos")


def entornos_base():
    return ("usuarios_db", "eventos_db", "compras_db", "usuarios", "eventos", "compras1", "compras2",
            "compras_lb", "frontend")

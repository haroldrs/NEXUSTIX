#!/usr/bin/env python3
"""Pruebas reproducibles del Punto 3. Ejecutar desde la raíz Project/.

Las pruebas detienen y reinician contenedores auxiliares. No borra datos ni
realiza compras exitosas; el fallo grupal está diseñado para compensarse.
"""
import json
import subprocess
import sys
import time
from typing import Any

import requests

BASE = "http://localhost"
TIMEOUT = 5


def check(ok: bool, message: str) -> None:
    print(f"[{'OK' if ok else 'ERROR'}] {message}")
    if not ok:
        raise AssertionError(message)


def get_json(url: str) -> tuple[int, Any]:
    r = requests.get(url, timeout=TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        data = r.text
    return r.status_code, data


def post_json(url: str, payload: dict | None = None) -> tuple[int, Any]:
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        data = r.text
    return r.status_code, data


def compose(action: str, service: str) -> None:
    subprocess.run(["docker", "compose", action, service], check=True)


def wait_health(url: str, expected: bool = True, seconds: int = 30) -> None:
    end = time.time() + seconds
    last = "sin respuesta"
    while time.time() < end:
        try:
            code, data = get_json(url)
            healthy = code == 200 and isinstance(data, dict) and data.get("status") == "ok"
            if healthy == expected:
                return
            last = f"HTTP {code}: {data}"
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(1)
    raise AssertionError(f"No se alcanzó estado expected={expected} en {url}: {last}")


def audit() -> dict:
    code, data = get_json(f"{BASE}:8005/auditoria/consistencia")
    check(code == 200, f"Auditoría disponible (HTTP {code})")
    check(data.get("todo_consistente") is True, "Auditoría reporta todo_consistente=true")
    check(data.get("balance_global_diferencia") == 0, "Balance global de stock = 0")
    check(data.get("totales", {}).get("reservas_pendientes") == 0, "No hay reservas pendientes")
    return data


def test_replica() -> None:
    print("\n=== 1. Caída de una réplica ===")
    before = audit()
    compose("stop", "compras1")
    try:
        wait_health(f"{BASE}:8004/health")
        for i in range(5):
            code, data = get_json(f"{BASE}:8005/health")
            check(code == 200 and data.get("status") == "ok", f"Balanceador responde durante caída ({i+1}/5)")
        after = audit()
        check(after["totales"] == before["totales"], "Stock e historial no cambian por detener una réplica")
    finally:
        compose("start", "compras1")
        wait_health(f"{BASE}:8003/health")
    print("Réplica restaurada.")


def test_interrupted_group() -> None:
    print("\n=== 2. Interrupción simulada de transacción grupal ===")
    before = audit()
    payload = {
        "usuario_id": 1,
        "items": [{"evento_id": 1, "cantidad": 1}, {"evento_id": 3, "cantidad": 1}],
        "tipo_imprevisto": "apagon_maquina",
    }
    code, data = post_json(f"{BASE}:8005/compras/grupal", payload)
    check(code == 409, f"Lote abortado con HTTP 409 (recibido {code})")
    detail = data.get("detail", {}) if isinstance(data, dict) else {}
    check(detail.get("decision_consenso") == "GLOBAL_ABORT", "Resultado GLOBAL_ABORT")
    after = audit()
    check(after["totales"] == before["totales"], "Stock e historial conservados tras compensación")
    print("Nota: es un fallo inyectado dentro del flujo, no un SIGKILL real del proceso.")


def test_dependency(service: str, port: int, description: str) -> None:
    print(f"\n=== 3. Caída de dependencia: {description} ===")
    before = audit()
    compose("stop", service)
    try:
        payload = {"usuario_id": 1, "evento_id": 3, "cantidad": 1}
        code, data = post_json(f"{BASE}:8003/compras", payload)
        check(code in (502, 503, 504), f"Compra rechazada por dependencia caída (HTTP {code})")
        if isinstance(data, dict):
            detail = data.get("detail", {})
            if isinstance(detail, dict):
                check(detail.get("decision_consenso", "GLOBAL_ABORT") == "GLOBAL_ABORT", "No se confirma la compra")
    finally:
        compose("start", service)
        wait_health(f"{BASE}:{port}/health")
    after = audit()
    check(after["totales"] == before["totales"], "Stock e historial iguales después de recuperar la dependencia")


def test_persistent_journal() -> None:
    print("\n=== 4. Bitácora persistente compartida ===")
    code1, one = get_json(f"{BASE}:8003/compras/journal")
    code2, two = get_json(f"{BASE}:8004/compras/journal")
    check(code1 == code2 == 200, "Bitácora consultable desde ambas réplicas")
    check(one.get("persistente") is True and two.get("persistente") is True, "API indica persistencia en PostgreSQL")
    check(one.get("total_entradas") == two.get("total_entradas"), "Ambas réplicas ven el mismo total de eventos")


def main() -> int:
    print("PUNTO 3 — tolerancia a fallos y recuperación")
    try:
        wait_health(f"{BASE}:8002/health")
        wait_health(f"{BASE}:8001/health")
        wait_health(f"{BASE}:8003/health")
        wait_health(f"{BASE}:8004/health")
        audit()
        test_replica()
        test_interrupted_group()
        test_dependency("usuarios", 8001, "Usuarios")
        test_dependency("eventos", 8002, "Eventos")
        test_persistent_journal()
        audit()
    except (AssertionError, requests.RequestException, subprocess.CalledProcessError) as exc:
        print(f"\nPRUEBA DETENIDA: {exc}", file=sys.stderr)
        return 1
    print("\nRESULTADO: todas las comprobaciones automatizadas finalizaron correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

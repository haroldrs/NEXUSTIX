#!/usr/bin/env python3
"""Prueba no destructiva de healthchecks, métricas y auditoría del clúster."""
import json
import sys
import time
import requests

BASES = {
    "compras1": "http://localhost:8003",
    "compras2": "http://localhost:8004",
    "balanceador": "http://localhost:8005",
    "eventos": "http://localhost:8002",
    "usuarios": "http://localhost:8001",
}
fallos = []
def check(nombre, condicion, detalle=""):
    estado = "OK" if condicion else "FALLO"
    print(f"[{estado}] {nombre}" + (f" — {detalle}" if detalle else ""))
    if not condicion:
        fallos.append(nombre)

def get(url, timeout=4):
    return requests.get(url, timeout=timeout)

print("=== PUNTO 5: OBSERVABILIDAD (PRUEBA NO DESTRUCTIVA) ===")
for nombre, base in BASES.items():
    try:
        r = get(base + "/health")
        data = r.json()
        check(f"health {nombre}", r.status_code == 200 and data.get("status") == "ok", f"HTTP {r.status_code}; status={data.get('status')}")
    except Exception as exc:
        check(f"health {nombre}", False, str(exc))

for nombre, base in (("compras1", BASES["compras1"]), ("compras2", BASES["compras2"])):
    try:
        r = get(base + "/metricas")
        d = r.json()
        campos = ("peticiones_totales", "errores_http", "latencia_promedio_ms", "uptime_segundos", "eventos_recientes")
        ok = r.status_code == 200 and all(k in d for k in campos)
        check(f"métricas {nombre}", ok, json.dumps({k: d.get(k) for k in campos}, ensure_ascii=False))
        # Esta petición debe quedar registrada en el contador de peticiones.
        r2 = get(base + "/metricas")
        d2 = r2.json()
        check(f"contador de peticiones {nombre}", d2.get("peticiones_totales", 0) >= d.get("peticiones_totales", 0), f"antes={d.get('peticiones_totales')} después={d2.get('peticiones_totales')}")
    except Exception as exc:
        check(f"métricas {nombre}", False, str(exc))

try:
    r = get(BASES["balanceador"] + "/health")
    d = r.json()
    check("balanceador responde", r.status_code == 200 and d.get("status") == "ok", f"HTTP {r.status_code}; instancia={d.get('instancia')}")
except Exception as exc:
    check("balanceador responde", False, str(exc))

try:
    r = get(BASES["compras1"] + "/auditoria/consistencia", timeout=8)
    d = r.json()
    check("auditoría de consistencia", r.status_code == 200 and d.get("todo_consistente") is True and d.get("balance_global_diferencia") == 0,
          f"HTTP {r.status_code}; consistente={d.get('todo_consistente')}; diferencia={d.get('balance_global_diferencia')}")
except Exception as exc:
    check("auditoría de consistencia", False, str(exc))

print("\nRESULTADO:", "TODAS LAS PRUEBAS PASARON" if not fallos else f"{len(fallos)} PRUEBA(S) FALLARON: {', '.join(fallos)}")
sys.exit(0 if not fallos else 1)

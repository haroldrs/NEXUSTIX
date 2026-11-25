#!/usr/bin/env python3
"""Prueba de concurrencia distribuida: muchos compradores, pocos asientos.

Crea un evento con CAPACIDAD lugares y lanza COMPRADORES compras simultáneas,
repartidas entre uno o varios gateways (por ejemplo, uno por cada PC). Al
final comprueba con datos del servidor que:

  * se vendieron exactamente CAPACIDAD entradas (ni una más),
  * el resto recibió "sin disponibilidad",
  * el inventario cumple inicial = disponibles + reservadas + vendidas.

Uso (cada --gateway es la IP o nombre de una PC con gateway):
  python3 scripts/prueba_concurrencia.py --gateway 100.101.1.11 --gateway 100.101.1.12 \
      --admin-email admin@example.com --admin-password-file ~/.config/nexustix/demo-secrets/admin-password

Solo usar en un entorno de pruebas: crea un evento y usuarios sintéticos.
"""
from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PORTS = {"usuarios": 8001, "eventos": 8002, "compras": 8005}


def api(host, service, path):
    return f"http://{host}:{PORTS[service]}{path}"


def session():
    s = requests.Session()
    s.trust_env = False
    return s


def login(host, email, password):
    r = session().post(api(host, "usuarios", "/auth/login"), json={"email": email, "password": password},
                       headers={"X-CSRF-Protection": "1"}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def register(host, index, run):
    email = f"comprador{index}-{run}@example.com"
    r = session().post(api(host, "usuarios", "/auth/register"),
                       json={"nombre": f"Comprador {index}", "email": email, "password": secrets.token_urlsafe(16)},
                       headers={"X-CSRF-Protection": "1"}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def buy(host, token, event_id, barrier):
    headers = {"Authorization": f"Bearer {token}"}
    s = session()
    barrier.wait()                      # todos salen al mismo tiempo
    started = time.monotonic()
    r = s.post(api(host, "compras", "/ordenes"), json={"items": [{"evento_id": event_id, "cantidad": 1}]},
               headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, timeout=30)
    if r.status_code >= 300:
        return {"gateway": host, "resultado": "rechazada", "http": r.status_code, "ms": round((time.monotonic() - started) * 1000)}
    order = r.json()
    for _ in range(40):
        if order.get("estado") != "reservando":
            break
        time.sleep(0.25)
        order = s.get(api(host, "compras", f"/ordenes/{order['id']}"), headers=headers, timeout=10).json()
    if order.get("estado") != "reservada":
        return {"gateway": host, "resultado": "sin_disponibilidad", "estado": order.get("estado"), "ms": round((time.monotonic() - started) * 1000)}
    paid = s.post(api(host, "compras", f"/ordenes/{order['id']}/pagar"), json={"resultado": "aprobado"},
                  headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, timeout=30)
    final = paid.json().get("estado") if paid.ok else f"http {paid.status_code}"
    return {"gateway": host, "resultado": "comprada" if final == "completada" else final, "ms": round((time.monotonic() - started) * 1000)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateway", action="append", required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--admin-password-file", required=True, type=Path)
    parser.add_argument("--capacidad", type=int, default=5)
    parser.add_argument("--compradores", type=int, default=30)
    parser.add_argument("--salida", type=Path)
    args = parser.parse_args()
    run = uuid.uuid4().hex[:6]
    first = args.gateway[0]
    admin = login(first, args.admin_email, args.admin_password_file.expanduser().read_text().strip())
    event = session().post(api(first, "eventos", "/eventos"), headers={"Authorization": f"Bearer {admin}"}, timeout=15, json={
        "nombre": f"Prueba de concurrencia {run}", "lugar": "Laboratorio", "fecha": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "capacidad": args.capacidad, "precio_centimos": 1000})
    event.raise_for_status()
    event_id = event.json()["id"]
    print(f"Evento {event_id} con {args.capacidad} lugares; registrando {args.compradores} compradores…")
    tokens = [register(args.gateway[i % len(args.gateway)], i, run) for i in range(args.compradores)]
    barrier = threading.Barrier(args.compradores)
    with ThreadPoolExecutor(max_workers=args.compradores) as pool:
        results = list(pool.map(lambda i: buy(args.gateway[i % len(args.gateway)], tokens[i], event_id, barrier), range(args.compradores)))
    inventory = session().get(api(first, "eventos", "/inventario/estado"), headers={"Authorization": f"Bearer {admin}"}, timeout=15).json()
    row = next(item for item in inventory["eventos"] if item["evento_id"] == event_id)
    sold = sum(item["resultado"] == "comprada" for item in results)
    summary = {
        "evento_id": event_id, "capacidad": args.capacidad, "compradores": args.compradores, "gateways": args.gateway,
        "compradas": sold, "sin_disponibilidad": sum(item["resultado"] in {"sin_disponibilidad", "cancelada"} for item in results),
        "otras": [item for item in results if item["resultado"] not in {"comprada", "sin_disponibilidad", "cancelada"}],
        "por_gateway": {host: sum(item["gateway"] == host and item["resultado"] == "comprada" for item in results) for host in args.gateway},
        "inventario": {key: row[key] for key in ("inicial", "disponible", "reservadas", "vendidas", "consistente")},
        "latencia_ms_max": max(item["ms"] for item in results),
    }
    summary["sin_sobreventa"] = sold <= args.capacidad and row["vendidas"] <= args.capacidad
    summary["aprobado"] = summary["sin_sobreventa"] and row["consistente"] and sold == args.capacidad
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.salida:
        args.salida.write_text(text + "\n")
    raise SystemExit(0 if summary["aprobado"] else 1)


if __name__ == "__main__":
    main()

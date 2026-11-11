#!/usr/bin/env python3
"""Verificación HTTP de una instalación generada con ``nodos.py preparar``.

Solo usa la biblioteca estándar y habla con los gateways por HTTP: no ejecuta
Docker, no detiene servicios ni borra datos. Crea datos SINTÉTICOS (eventos
``QA ...``, compradores ``qa-nodos-*@example.com`` y sus compras), que quedan
como evidencia.

Pasos:
  salud        /lb-health y /health de cada gateway; qué nodos atienden.
  eventos      crea eventos de prueba con el administrador (--crear-eventos).
  compra       registro, orden idempotente, pago, entrada QR y validación.
  concurrencia dos compradores por la última entrada: solo uno gana.
  replicacion  cada base primaria informa sus standbys en streaming.
  inventario   ecuación inicial = disponible + reservadas + vendidas.

Ejemplo, desde la raíz del repositorio en la PC administradora:

  python3 despliegue-nodos/verificar.py \\
      --entrega "$HOME/.config/nexustix/nodos-dos-v4" \\
      --admin-email admin@example.com --crear-eventos

Para probar un fallo, detenga un servicio en la OTRA PC y repita con
``--gateway http://IP:8080`` del nodo que sigue vivo (ver README).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PORTS = {"frontend": 8080, "usuarios": 8001, "eventos": 8002, "compras": 8005}


class Fallo(AssertionError):
    pass


def request(method, url, body=None, token=None, headers=None, timeout=35):
    data = None if body is None else json.dumps(body).encode()
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, _json(raw), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read().decode("utf-8", "replace")), exc.headers
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, {"error": str(getattr(exc, "reason", exc))}, {}


def _json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:300]}


def check(condition, message):
    if not condition:
        raise Fallo(message)
    print(f"  OK  {message}")


class Gateway:
    def __init__(self, base):
        parsed = urllib.parse.urlsplit(base if "://" in base else f"http://{base}")
        self.host = parsed.hostname
        self.ports = dict(PORTS)
        if parsed.port:
            self.ports["frontend"] = parsed.port

    def url(self, service, path):
        return f"http://{self.host}:{self.ports[service]}{path}"


def load_gateways(args):
    if args.gateway:
        return [Gateway(url) for url in args.gateway]
    if not args.entrega:
        raise Fallo("Indique --entrega o al menos un --gateway")
    points = json.loads((args.entrega / "access-points.json").read_text(encoding="utf-8"))
    gateways = []
    for point in sorted(points, key=lambda p: not p.get("preferred")):
        gateway = Gateway(point["url"])
        for service in ("usuarios", "eventos", "compras"):
            port = urllib.parse.urlsplit(point[f"{service}_url"]).port
            if port:
                gateway.ports[service] = port
        gateways.append(gateway)
    return gateways


def paso_salud(gateways, muestras):
    print("[1] Salud de gateways y reparto entre nodos")
    vivos = []
    for gateway in gateways:
        status, body, _ = request("GET", gateway.url("frontend", "/lb-health"), timeout=5)
        if status != 200:
            print(f"  --  gateway {gateway.host} no responde ({body.get('error') if isinstance(body, dict) else status})")
            continue
        print(f"  OK  gateway {gateway.host} (nodo {body.get('node_id')})")
        vivos.append(gateway)
        for service in ("usuarios", "eventos", "compras"):
            backends = {}
            for _ in range(muestras):
                status, body, headers = request("GET", gateway.url(service, "/health"), timeout=8)
                name = headers.get("X-Nexustix-Backend", "?") if status == 200 else f"HTTP {status}"
                backends[name] = backends.get(name, 0) + 1
            ok = all(not name.startswith("HTTP") for name in backends)
            reparto = ", ".join(f"{name}×{count}" for name, count in sorted(backends.items()))
            check(ok, f"{service:8} vía {gateway.host}: {reparto}")
    check(bool(vivos), f"{len(vivos)} de {len(gateways)} gateways disponibles")
    return vivos[0]


def login_admin(gw, email, password):
    status, body, _ = request("POST", gw.url("usuarios", "/auth/login"), {"email": email, "password": password})
    check(status == 200 and isinstance(body, dict) and body.get("access_token"),
          f"login del administrador {email} (HTTP {status})")
    return body["access_token"]


def nuevo_comprador(gw):
    email = f"qa-nodos-{uuid.uuid4().hex[:12]}@example.com"
    status, body, _ = request("POST", gw.url("usuarios", "/auth/register"),
                              {"nombre": "QA nodos", "email": email, "password": secrets.token_urlsafe(24)})
    check(status == 201 and isinstance(body, dict) and body.get("access_token"), f"comprador sintético {email}")
    return body["access_token"]


def crear_evento(gw, admin, nombre, capacidad, precio=2500):
    fecha = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
    status, body, headers = request("POST", gw.url("eventos", "/eventos"), {
        "nombre": nombre, "descripcion": "Evento sintético de verificación multinodo",
        "lugar": "Laboratorio LAN", "categoria": "QA", "fecha": fecha,
        "capacidad": capacidad, "precio_centimos": precio}, token=admin)
    check(status == 201 and isinstance(body, dict) and body.get("id"),
          f"evento #{body.get('id') if isinstance(body, dict) else '?'} '{nombre}' capacidad {capacidad} "
          f"(atendió {headers.get('X-Nexustix-Backend', '?')})")
    return body


def paso_eventos(gw, admin, crear):
    print("[2] Catálogo")
    if crear:
        for nombre, capacidad, precio in (("QA Concierto LAN", 200, 4500), ("QA Festival Distribuido", 100, 3000),
                                          ("QA Teatro Réplica", 50, 2000)):
            crear_evento(gw, admin, nombre, capacidad, precio)
    status, eventos, _ = request("GET", gw.url("eventos", "/eventos"))
    check(status == 200 and isinstance(eventos, list), f"{len(eventos or [])} eventos publicados")
    return eventos


def stock(gw, evento_id):
    status, body, _ = request("GET", gw.url("eventos", f"/eventos/{evento_id}"))
    if status != 200 or not isinstance(body, dict):
        raise Fallo(f"no se pudo leer el evento {evento_id} (HTTP {status})")
    return int(body["stock"])


def orden(gw, token, items, key=None):
    return request("POST", gw.url("compras", "/ordenes"), {"items": items}, token=token,
                   headers={"Idempotency-Key": key or uuid.uuid4().hex})


def paso_compra(gw, admin):
    print("[3] Compra completa")
    evento = crear_evento(gw, admin, "QA Compra individual", 10)
    comprador = nuevo_comprador(gw)
    antes = stock(gw, evento["id"])
    key = uuid.uuid4().hex
    items = [{"evento_id": evento["id"], "cantidad": 2}]
    status, primera, headers = orden(gw, comprador, items, key)
    check(status == 201 and isinstance(primera, dict) and primera.get("id"),
          f"orden {primera.get('id', '?')[:8] if isinstance(primera, dict) else '?'} reservada "
          f"(atendió {headers.get('X-Nexustix-Backend', '?')})")
    status, segunda, headers = orden(gw, comprador, items, key)
    check(status == 201 and segunda.get("id") == primera["id"],
          f"reintento con la misma clave devuelve la misma orden (atendió {headers.get('X-Nexustix-Backend', '?')})")
    status, pago, _ = request("POST", gw.url("compras", f"/ordenes/{primera['id']}/pagar"),
                              {"resultado": "aprobado"}, token=comprador)
    check(status == 200 and pago.get("estado") == "completada", f"pago aprobado, orden {pago.get('estado')}")
    status, pago, _ = request("POST", gw.url("compras", f"/ordenes/{primera['id']}/pagar"),
                              {"resultado": "aprobado"}, token=comprador)
    check(status == 200 and pago.get("estado") == "completada", "pagar dos veces no cobra dos veces")
    despues = stock(gw, evento["id"])
    check(despues == antes - 2, f"stock {antes} -> {despues}")
    status, entradas, _ = request("GET", gw.url("compras", "/entradas/mias"), token=comprador)
    mias = [t for t in (entradas or []) if t.get("orden_id") == primera["id"]]
    check(status == 200 and len(mias) == 2, f"{len(mias)} entradas QR emitidas")
    acceso = {"qr": mias[0]["qr"], "evento_id": mias[0]["evento_id"]}
    status, _, _ = request("POST", gw.url("compras", "/entradas/validar"), acceso, token=admin)
    check(status == 200, "validación de la entrada en puerta: acceso concedido")
    status, _, _ = request("POST", gw.url("compras", "/entradas/validar"), acceso, token=admin)
    check(status == 409, "segunda validación de la misma entrada: rechazada (409)")

    print("[4] Pago rechazado devuelve el stock")
    status, rechazada, _ = orden(gw, comprador, [{"evento_id": evento["id"], "cantidad": 1}])
    check(status == 201, "orden de 1 entrada reservada")
    status, pago, _ = request("POST", gw.url("compras", f"/ordenes/{rechazada['id']}/pagar"),
                              {"resultado": "rechazado"}, token=comprador)
    check(status == 200 and pago.get("estado") == "cancelada", "pago rechazado: orden cancelada")
    check(stock(gw, evento["id"]) == despues, "el stock vuelve a su valor anterior")


def paso_concurrencia(gw, admin):
    print("[5] Última entrada con dos compradores a la vez")
    evento = crear_evento(gw, admin, "QA Última entrada", 1)
    compradores = [nuevo_comprador(gw), nuevo_comprador(gw)]
    resultados = [None, None]
    barrera = threading.Barrier(2)

    def comprar(i):
        barrera.wait()
        resultados[i] = orden(gw, compradores[i], [{"evento_id": evento["id"], "cantidad": 1}])

    hilos = [threading.Thread(target=comprar, args=(i,)) for i in range(2)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    estados = []
    for status, body, headers in resultados:
        estado = body.get("estado") if status == 201 and isinstance(body, dict) else f"HTTP {status}"
        estados.append(estado)
        print(f"      {estado:10} atendida por {headers.get('X-Nexustix-Backend', '?')}")
    ganadoras = [i for i, e in enumerate(estados) if e == "reservada"]
    check(len(ganadoras) == 1, f"exactamente una reserva gana ({', '.join(map(str, estados))})")
    ganadora = resultados[ganadoras[0]][1]
    status, pago, _ = request("POST", gw.url("compras", f"/ordenes/{ganadora['id']}/pagar"),
                              {"resultado": "aprobado"}, token=compradores[ganadoras[0]])
    check(status == 200 and pago.get("estado") == "completada", "la ganadora paga y completa")
    check(stock(gw, evento["id"]) == 0, "stock final 0, sin sobreventa")


def paso_replicacion(gw, admin, standbys_esperados):
    print("[6] Replicación PostgreSQL")
    for service in ("usuarios", "eventos", "compras"):
        status, body, headers = request("GET", gw.url(service, "/diagnostico/db"), token=admin)
        check(status == 200 and isinstance(body, dict) and body.get("rol") == "primario",
              f"{service}: la API escribe en un primario (vía {headers.get('X-Nexustix-Backend', '?')})")
        replicas = body.get("replicas") or []
        for replica in replicas:
            print(f"      standby {replica.get('application_name')}: {replica.get('state')}, "
                  f"lag {replica.get('lag_bytes')} bytes")
        streaming = [r for r in replicas if r.get("state") == "streaming"]
        check(len(streaming) >= standbys_esperados, f"{service}: {len(streaming)} standby(s) en streaming")


def paso_inventario(gw, admin):
    print("[7] Inventario")
    status, body, _ = request("GET", gw.url("eventos", "/inventario/estado"), token=admin)
    check(status == 200 and isinstance(body, dict), "auditoría de inventario disponible")
    malos = [e for e in body.get("eventos", []) if not e.get("consistente")]
    for evento in malos:
        print(f"      inconsistente: #{evento['evento_id']} {evento['nombre']}")
    check(body.get("consistente") is True, f"{len(body.get('eventos', []))} eventos consistentes")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entrega", type=Path, help="Carpeta de la entrega (lee access-points.json y master/admin-password)")
    parser.add_argument("--gateway", action="append", help="URL de portal, p. ej. http://192.168.1.27:8080 (repetible)")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--admin-password-file", type=Path, help="Por defecto <entrega>/master/admin-password")
    parser.add_argument("--crear-eventos", action="store_true", help="Crea tres eventos de muestra en el catálogo")
    parser.add_argument("--solo", choices=["salud", "eventos", "compra", "concurrencia", "replicacion", "inventario"],
                        action="append", help="Ejecuta solo los pasos indicados (repetible)")
    parser.add_argument("--standbys", type=int, default=1, help="Standbys en streaming esperados por base (0 en un fallo)")
    parser.add_argument("--muestras", type=int, default=6, help="Peticiones /health por servicio para ver el reparto")
    args = parser.parse_args(argv)
    pasos = set(args.solo or ["salud", "eventos", "compra", "concurrencia", "replicacion", "inventario"])
    resumen = []
    try:
        gw = paso_salud(load_gateways(args), args.muestras)
        print(f"\nSe usa el gateway {gw.host} para los pasos siguientes.")
        admin = None
        if pasos - {"salud"}:
            password_file = args.admin_password_file or (args.entrega / "master" / "admin-password" if args.entrega else None)
            if not password_file:
                raise Fallo("Indique --admin-password-file o --entrega")
            admin = login_admin(gw, args.admin_email, password_file.read_text(encoding="utf-8").strip())
        for nombre, accion in (("eventos", lambda: paso_eventos(gw, admin, args.crear_eventos)),
                               ("compra", lambda: paso_compra(gw, admin)),
                               ("concurrencia", lambda: paso_concurrencia(gw, admin)),
                               ("replicacion", lambda: paso_replicacion(gw, admin, args.standbys)),
                               ("inventario", lambda: paso_inventario(gw, admin))):
            if nombre in pasos:
                accion()
                resumen.append(nombre)
    except (Fallo, OSError, KeyError, ValueError) as exc:
        print(f"\nVERIFICACIÓN FALLIDA: {exc}", file=sys.stderr)
        if resumen:
            print(f"Pasos aprobados antes del fallo: {', '.join(resumen)}", file=sys.stderr)
        return 1
    print(f"\nVERIFICACIÓN APROBADA ({time.strftime('%Y-%m-%d %H:%M:%S')}): salud, {', '.join(resumen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

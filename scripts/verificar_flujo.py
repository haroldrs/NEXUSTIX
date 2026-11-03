#!/usr/bin/env python3
"""Verificación del flujo principal de NEXUSTIX (solo biblioteca estándar).

Sin --compra (solo lectura): salud de los seis servicios HTTP, catálogo de
Eventos y que el balanceador alcanza a Compras.

Con --compra (modifica datos; pensado para el entorno de pruebas):
 1. registra un comprador sintético (qa-verificacion-*@example.com);
 2. compra 1 entrada por el balanceador con Idempotency-Key;
 3. repite la misma petición (replay idempotente, misma compra);
 4. reutiliza la clave con otro cuerpo (debe responder 409);
 5. comprueba en PostgreSQL: una sola compra para la operación, su clave
    idempotente 'completed' y la reserva 'confirmada' en Eventos;
 6. comprueba que el stock bajó exactamente 1 y aparece en "Mis compras".
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entornos  # noqa: E402
import pg_docker  # noqa: E402


class Fallo(AssertionError):
    pass


def request(method: str, url: str, body: dict | None = None, headers: dict | None = None,
            timeout: float = 35):
    """(status, json, headers); headers es un HTTPMessage (búsqueda sin distinguir mayúsculas)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else None), response.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = {"raw": raw[:300]}
        return exc.code, payload, exc.headers


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Fallo(message)
    print(f"  OK  {message}")


def verificar_lectura(entorno: entornos.Entorno) -> list[dict]:
    print(f"[1] Salud de servicios del entorno {entorno.nombre}")
    for service in ("usuarios", "eventos", "compras1", "compras2"):
        status, body, _ = request("GET", f"{entorno.url(service)}/health", timeout=8)
        check(status == 200 and isinstance(body, dict) and body.get("status") == "ok" and body.get("db") == "connected",
              f"{service} /health 200 con base conectada")
    status, body, headers = request("GET", f"{entorno.url('compras_lb')}/health", timeout=8)
    check(status == 200 and isinstance(body, dict) and body.get("servicio") == "compras",
          f"balanceador alcanza Compras (instancia {body.get('instancia') if isinstance(body, dict) else '?'})")
    status, _, _ = request("GET", f"{entorno.url('frontend')}/health", timeout=8)
    check(status == 200, "frontend responde")
    print("[2] Catálogo")
    status, eventos, _ = request("GET", f"{entorno.url('eventos')}/eventos", timeout=8)
    check(status == 200 and isinstance(eventos, list) and len(eventos) >= 1, f"{len(eventos or [])} eventos publicados")
    return eventos


def verificar_compra(entorno: entornos.Entorno, eventos: list[dict]) -> None:
    print("[3] Comprador sintético")
    email = f"qa-verificacion-{uuid.uuid4().hex[:12]}@example.com"
    password = secrets.token_urlsafe(24)
    status, auth, _ = request("POST", f"{entorno.url('usuarios')}/auth/register",
                              {"nombre": "QA verificación", "email": email, "password": password})
    check(status == 201 and isinstance(auth, dict) and auth.get("access_token"), "registro y token de acceso")
    bearer = {"Authorization": f"Bearer {auth['access_token']}"}
    evento = next((e for e in eventos if int(e.get("stock", 0)) >= 2), None)
    check(evento is not None, "existe un evento con stock suficiente")
    evento_id = int(evento["id"])
    _, before, _ = request("GET", f"{entorno.url('eventos')}/eventos/{evento_id}/stock")

    print("[4] Compra por el balanceador")
    key = f"verificacion-{uuid.uuid4().hex}"
    body = {"evento_id": evento_id, "cantidad": 1}
    status, compra, headers = request("POST", f"{entorno.url('compras_lb')}/compras", body,
                                      {**bearer, "Idempotency-Key": key})
    check(status == 200 and isinstance(compra, dict) and compra.get("id"), f"compra aceptada (HTTP {status})")
    check(compra.get("confirmacion_reserva") == "confirmada", "reserva confirmada en Eventos")
    print(f"      atendida por {compra.get('atendido_por')} vía {headers.get('X-Nexustix-Upstream', '?')}")

    print("[5] Idempotencia")
    status, replay, headers = request("POST", f"{entorno.url('compras_lb')}/compras", body,
                                      {**bearer, "Idempotency-Key": key})
    check(status == 200 and isinstance(replay, dict) and replay.get("id") == compra["id"], "replay devuelve la misma compra")
    check(headers.get("Idempotency-Replayed") == "true", "replay marcado como Idempotency-Replayed")
    status, _, _ = request("POST", f"{entorno.url('compras_lb')}/compras", {"evento_id": evento_id, "cantidad": 2},
                           {**bearer, "Idempotency-Key": key})
    check(status == 409, "misma clave con otro contenido es rechazada (409)")

    print("[6] Persistencia en PostgreSQL")
    operacion = compra["operacion_id"]
    if not all(ch in "0123456789abcdef" for ch in operacion):
        raise Fallo("operacion_id con formato inesperado")
    compras_db = pg_docker.primario(entorno, "compras_db").container
    eventos_db = pg_docker.primario(entorno, "eventos_db").container
    filas = pg_docker.psql(compras_db, "compras_db", f"SELECT count(*) FROM compras WHERE operacion_id = '{operacion}'")
    check(filas == "1", "exactamente una fila en compras para la operación")
    estado_clave = pg_docker.psql(compras_db, "compras_db",
                                  f"SELECT state FROM purchase_idempotency WHERE operacion_id = '{operacion}'")
    check(estado_clave == "completed", "clave idempotente 'completed'")
    reserva = pg_docker.psql(eventos_db, "eventos_db",
                             f"SELECT estado || ':' || cantidad FROM reservas_stock WHERE operacion_id = '{operacion}'")
    check(reserva == "confirmada:1", "reserva 'confirmada' de 1 entrada en eventos_db")
    _, after, _ = request("GET", f"{entorno.url('eventos')}/eventos/{evento_id}/stock")
    check(isinstance(before, dict) and isinstance(after, dict) and after["stock"] == before["stock"] - 1,
          f"stock {before.get('stock')} -> {after.get('stock')} (bajó exactamente 1; ejecutar sin otras compras simultáneas)")
    status, mias, _ = request("GET", f"{entorno.url('compras_lb')}/compras/mias", headers=bearer)
    check(status == 200 and any(c.get("id") == compra["id"] for c in mias or []), "aparece en Mis compras")
    print(f"\nComprador sintético {email} y compra #{compra['id']} quedan registrados como evidencia de prueba.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entorno", required=True, choices=sorted(entornos.ENTORNOS))
    parser.add_argument("--compra", action="store_true")
    args = parser.parse_args(argv)
    entorno = entornos.get(args.entorno)
    try:
        eventos = verificar_lectura(entorno)
        if args.compra:
            verificar_compra(entorno, eventos)
    except (Fallo, pg_docker.PgError, RuntimeError, OSError) as exc:
        print(f"\nVERIFICACIÓN FALLIDA: {exc}", file=sys.stderr)
        return 1
    print("\nVERIFICACIÓN APROBADA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

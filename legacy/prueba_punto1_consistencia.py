#!/usr/bin/env python3
"""Pruebas no destructivas de validación, reserva idempotente y auditoría.

Requiere que el proyecto esté levantado. La prueba de reserva usa un operation_id
nuevo y libera la misma operación al final, por lo que no debe consumir stock neto.
Ejecutar sin Locust ni otras compras simultáneas para comparar stock con precisión.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


def request(method, url, payload=None, expected=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            status, raw = response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode()
    except Exception as exc:
        raise RuntimeError(f"No se pudo consultar {url}: {exc}") from exc
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"raw": raw}
    if expected is not None and status not in expected:
        raise AssertionError(f"{method} {url}: HTTP {status}, esperado {expected}; respuesta={body}")
    return status, body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compras", default="http://localhost:8005", help="URL base del balanceador de Compras")
    parser.add_argument("--eventos", default="http://localhost:8002", help="URL base del servicio Eventos")
    parser.add_argument("--evento-id", type=int, default=1)
    parser.add_argument("--require-consistent", action="store_true", help="Termina con error si la auditoría detecta discrepancias históricas")
    args = parser.parse_args()
    checks = []

    # API valida antes de procesar; no debe reservar stock ni registrar compras.
    for cantidad in (0, -1):
        status, body = request("POST", f"{args.compras}/compras", {
            "usuario_id": 1, "evento_id": args.evento_id, "cantidad": cantidad
        }, expected={422})
        checks.append(f"PASS cantidad={cantidad}: compra rechazada con HTTP {status}")

    # El endpoint de reserva también debe rechazar cantidades no positivas.
    for cantidad in (0, -1):
        params = urllib.parse.urlencode({"cantidad": cantidad, "operacion_id": uuid.uuid4().hex})
        status, body = request("POST", f"{args.eventos}/eventos/{args.evento_id}/reservar?{params}", expected={422})
        checks.append(f"PASS cantidad={cantidad}: reserva rechazada con HTTP {status}")

    # Reserva idempotente: repetir la misma clave no descuenta stock dos veces.
    _, before = request("GET", f"{args.eventos}/eventos/{args.evento_id}/stock", expected={200})
    stock_before = int(before["stock"])
    operation_id = uuid.uuid4().hex
    query = urllib.parse.urlencode({"cantidad": 1, "operacion_id": operation_id})
    _, first = request("POST", f"{args.eventos}/eventos/{args.evento_id}/reservar?{query}", expected={200})
    _, after_first = request("GET", f"{args.eventos}/eventos/{args.evento_id}/stock", expected={200})
    stock_after_first = int(after_first["stock"])
    assert stock_after_first == stock_before - 1, f"Primera reserva: stock esperado {stock_before - 1}, observado {stock_after_first}"

    _, retry = request("POST", f"{args.eventos}/eventos/{args.evento_id}/reservar?{query}", expected={200})
    _, after_retry = request("GET", f"{args.eventos}/eventos/{args.evento_id}/stock", expected={200})
    stock_after_retry = int(after_retry["stock"])
    assert stock_after_retry == stock_after_first, "El reintento descontó stock por segunda vez"
    assert retry.get("idempotente") is True, "El segundo intento no se identificó como idempotente"
    checks.append("PASS reserva repetida con la misma operacion_id: no descuenta dos veces")

    release_query = urllib.parse.urlencode({"cantidad": 1, "operacion_id": operation_id})
    request("POST", f"{args.eventos}/eventos/{args.evento_id}/liberar?{release_query}", expected={200})
    _, after_release = request("GET", f"{args.eventos}/eventos/{args.evento_id}/stock", expected={200})
    stock_after_release = int(after_release["stock"])
    assert stock_after_release == stock_before, f"La compensación no restauró el stock inicial: {stock_after_release} != {stock_before}"
    request("POST", f"{args.eventos}/eventos/{args.evento_id}/liberar?{release_query}", expected={200})
    _, after_second_release = request("GET", f"{args.eventos}/eventos/{args.evento_id}/stock", expected={200})
    assert int(after_second_release["stock"]) == stock_before, "La segunda compensación incrementó el stock otra vez"
    checks.append("PASS compensación repetida: el stock se restaura una sola vez")

    status, audit = request("GET", f"{args.compras}/auditoria/consistencia", expected={200})
    checks.append(f"AUDITORÍA: todo_consistente={audit.get('todo_consistente')}; veredicto={audit.get('veredicto')}")
    checks.append("NOTA: compras antiguas sin operacion_id se auditan por la ecuación global de stock; las nuevas se correlacionan por operación.")

    print("\n".join(checks))
    print("\nResumen de auditoría:")
    print(json.dumps(audit, indent=2, ensure_ascii=False))
    if args.require_consistent and audit.get("todo_consistente") is not True:
        print("\nFAIL: la auditoría detectó inconsistencias o reservas pendientes.", file=sys.stderr)
        return 1
    print("\nPRUEBAS DE VALIDACIÓN E IDEMPOTENCIA: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

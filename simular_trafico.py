#!/usr/bin/env python3
"""Tráfico continuo de compras para demostrar el failover entre Compras1 y Compras2.

    python3 simular_trafico.py --entorno pruebas --evento 2 --compras 30
    # en otra terminal, mientras corre:
    python3 scripts/nx.py pruebas stop compras1      # y luego: start compras1

Cada compra usa una Idempotency-Key nueva. Si una petición falla por red o
responde 5xx, se reintenta con LA MISMA clave y el mismo cuerpo (nunca crea
una segunda compra). Al final muestra cuántas compras atendió cada réplica y
cuántas necesitaron reintento. Por defecto crea un comprador sintético; en el
entorno demo exige --permitir-demo porque modifica el stock real.
"""

from __future__ import annotations

import argparse
import collections
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import entornos  # noqa: E402


def call(method, url, body=None, headers=None, timeout=35):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null"), response.headers
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"null")
        except ValueError:
            payload = None
        return exc.code, payload, exc.headers
    except OSError as exc:
        return 0, {"error": str(exc)}, {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entorno", choices=sorted(entornos.ENTORNOS), default="pruebas")
    parser.add_argument("--evento", type=int, required=True, help="ID del evento a comprar (1 entrada por compra)")
    parser.add_argument("--compras", type=int, default=30)
    parser.add_argument("--pausa", type=float, default=1.0, help="segundos entre compras")
    parser.add_argument("--reintentos", type=int, default=3)
    parser.add_argument("--permitir-demo", action="store_true")
    args = parser.parse_args()
    entorno = entornos.get(args.entorno)
    if not entorno.datos_sinteticos and not args.permitir_demo:
        parser.error("En el entorno demo este script descuenta stock real; use --permitir-demo")

    email = f"qa-trafico-{uuid.uuid4().hex[:10]}@example.com"
    status, auth, _ = call("POST", f"{entorno.url('usuarios')}/auth/register",
                           {"nombre": "QA tráfico", "email": email, "password": secrets.token_urlsafe(24)})
    if status != 201:
        print(f"No se pudo crear el comprador sintético (HTTP {status})", file=sys.stderr)
        return 1
    bearer = {"Authorization": f"Bearer {auth['access_token']}"}
    lb = f"{entorno.url('compras_lb')}/compras"
    por_replica, resultados, con_reintento = collections.Counter(), collections.Counter(), 0
    print(f"Enviando {args.compras} compras al balanceador {lb} (Ctrl+C para detener)\n")
    try:
        for n in range(1, args.compras + 1):
            key = f"trafico-{uuid.uuid4().hex}"
            body = {"evento_id": args.evento, "cantidad": 1}
            for intento in range(1, args.reintentos + 2):
                started = time.perf_counter()
                status, data, headers = call("POST", lb, body, {**bearer, "Idempotency-Key": key})
                elapsed = (time.perf_counter() - started) * 1000
                if status == 0 or status >= 500:
                    print(f"#{n:03d} intento {intento}: {'sin conexión' if status == 0 else f'HTTP {status}'}"
                          f" ({elapsed:.0f} ms) -> reintento con la misma clave")
                    time.sleep(1.5)
                    continue
                break
            if intento > 1:
                con_reintento += 1
            replica = (data or {}).get("atendido_por", "-") if isinstance(data, dict) else "-"
            upstream = headers.get("X-Nexustix-Upstream", "-") if headers else "-"
            resultados[status] += 1
            if status == 200:
                por_replica[replica] += 1
                print(f"#{n:03d} HTTP 200 compra #{data.get('id')} · {replica} · upstream {upstream} · {elapsed:.0f} ms")
            else:
                detalle = (data or {}).get("detail") if isinstance(data, dict) else data
                print(f"#{n:03d} HTTP {status} · {str(detalle)[:90]}")
            time.sleep(args.pausa)
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
    print("\nResumen")
    print(f"  respuestas: {dict(resultados)}")
    print(f"  compras por réplica: {dict(por_replica)}")
    print(f"  compras que necesitaron reintento con la misma clave: {con_reintento}")
    print(f"  comprador sintético: {email}")
    print("Compruebe la consistencia con la auditoría (panel Observabilidad o tests/resiliencia).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

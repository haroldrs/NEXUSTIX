#!/usr/bin/env python3
"""HTTP y fallos reales SOLO en nexustix-distributed-lab, puertos 28080–28085.

Crea usuarios/eventos sintéticos, conserva datos, no elimina volúmenes.
--fallos permite detener/reanudar réplicas de este laboratorio nuevo.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

import requests

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "nexustix-distributed-lab"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--secrets-dir", type=Path, required=True)
    p.add_argument("--fallos", action="store_true")
    args = p.parse_args()
    compose = ["docker", "compose", "--env-file", str(args.env_file.resolve()), "--project-name", PROJECT,
               "-f", str(ROOT / "docker-compose.distribuido.yml")]
    def command(*cmd):
        return subprocess.run([*compose, *cmd], capture_output=True, text=True, check=True, timeout=180).stdout
    def fingerprint():
        ids = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=nexustix-auth-staging"], text=True).split()
        return sorted(subprocess.check_output(["docker", "inspect", "--format", "{{.Id}} {{.State.StartedAt}}", cid], text=True).strip() for cid in ids)
    before = fingerprint()
    names = command("ps", "--format", "json")
    assert PROJECT in names, "Laboratorio no disponible"
    command("exec", "-T", "usuarios", "python", "bootstrap_admin.py", "--email", "lab-admin@example.com",
            "--password-file", "/run/secrets/admin_password", "--if-missing")
    rows = []
    def check(name, action, expected, fn):
        try:
            fn()
            rows.append({"escenario": name, "inicial": "Laboratorio nuevo saludable con datos sintéticos",
                "accion": action, "esperado": expected, "obtenido": expected, "estado": "APROBADA"})
        except Exception as error:
            rows.append({"escenario": name, "accion": action, "esperado": expected,
                "obtenido": type(error).__name__, "estado": "FALLIDA"})
            raise
    def api(port, method, path, token=None, **kwargs):
        headers = kwargs.pop("headers", {})
        if token: headers["Authorization"] = f"Bearer {token}"
        return requests.request(method, f"http://127.0.0.1:{port}{path}", headers=headers, timeout=35, **kwargs)
    admin_response = api(28081, "POST", "/auth/login", json={"email": "lab-admin@example.com",
        "password": (args.secrets_dir / "admin-password").read_text().strip()})
    assert admin_response.status_code == 200, "Login administrativo falló"
    admin = admin_response.json()["access_token"]
    response = api(28081, "POST", "/auth/register", json={"nombre": "Comprador de laboratorio",
        "email": f"lab-{uuid4().hex[:12]}@example.com", "password": secrets.token_urlsafe(24)})
    assert response.status_code == 201
    buyer = response.json()["access_token"]
    def event(stock=5):
        r = api(28082, "POST", "/eventos", admin, json={"nombre": "HTTP sintético", "fecha": "2027-01-01T20:00:00-05:00",
            "capacidad": stock, "precio_centimos": 1000})
        assert r.status_code == 201
        return r.json()["id"]
    def order(items, key=None):
        r = api(28085, "POST", "/ordenes", buyer, headers={"Idempotency-Key": key or uuid4().hex}, json={"items": items})
        assert r.status_code == 201, f"Orden HTTP {r.status_code}"
        return r.json()
    def pay(oid, outcome="aprobado"):
        r = api(28085, "POST", f"/ordenes/{oid}/pagar", buyer, json={"resultado": outcome})
        assert r.status_code == 200
        return r.json()
    def single():
        oid = order([{"evento_id": event(), "cantidad": 1}])["id"]
        assert pay(oid)["estado"] == "completada"
        tickets = api(28085, "GET", "/entradas/mias", buyer).json()
        ticket = next(t for t in tickets if t["orden_id"] == oid)
        data = {"qr": ticket["qr"], "evento_id": ticket["evento_id"]}
        assert api(28085, "POST", "/entradas/validar", admin, json=data).status_code == 200
        assert api(28085, "POST", "/entradas/validar", admin, json=data).status_code == 409
    def group():
        key = uuid4().hex; items = [{"evento_id": event(), "cantidad": 2}, {"evento_id": event(), "cantidad": 1}]
        first, second = order(items, key), order(items, key)
        assert first["id"] == second["id"]
        assert pay(first["id"])["estado"] == "completada"
        assert pay(first["id"])["estado"] == "completada"
    def rejected():
        eid = event(1); oid = order([{"evento_id": eid, "cantidad": 1}])["id"]
        assert pay(oid, "rechazado")["estado"] == "cancelada"
        assert api(28082, "GET", f"/eventos/{eid}").json()["stock"] == 1
    def last_ticket():
        eid = event(1)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: order([{"evento_id": eid, "cantidad": 1}]), range(2)))
        assert sorted(o["estado"] for o in results) == ["cancelada", "reservada"]
        for o in results:
            if o["estado"] == "reservada":
                assert pay(o["id"])["estado"] == "completada"
    def replicas():
        for port in (28081, 28082, 28085):
            r = api(port, "GET", "/diagnostico/db", admin)
            assert r.status_code == 200
            assert r.json()["rol"] == "primario" and len(r.json()["replicas"]) == 1
            assert r.json()["replicas"][0]["state"] == "streaming"
    def failover():
        try:
            command("stop", "compras1")
            r = api(28085, "GET", "/health")
            assert r.status_code == 200 and r.json()["instancia"] == "2"
            group()
        finally:
            command("start", "compras1")
    def recovery():
        oid = order([{"evento_id": event(1), "cantidad": 1}])["id"]
        try:
            command("stop", "compras1", "compras2")
            # ID generado por el servidor (hex); solo ese pedido sintético.
            assert len(oid) == 32 and all(c in "0123456789abcdef" for c in oid)
            command("exec", "-T", "compras_db", "psql", "-U", "postgres", "-d", "compras_db", "-c",
                f"UPDATE ordenes SET expira_en=now()-interval '1 second' WHERE id='{oid}';")
        finally:
            command("start", "compras1", "compras2")
        deadline = time.monotonic()+45
        while time.monotonic() < deadline:
            r = api(28085, "GET", f"/ordenes/{oid}", buyer)
            if r.status_code == 200 and r.json()["estado"] == "cancelada": return
            time.sleep(1)
        raise AssertionError("Worker no recuperó la expiración")
    out = ROOT / "resultados" / ("http-distribuido-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True)
    try:
        check("Compra y QR duplicado", "Registrar, comprar, pagar y validar dos veces", "Compra completada; acceso 200, luego 409", single)
        check("Compra grupal idempotente", "Repetir pedido y pago", "Una orden con pago aprobado", group)
        check("Pago rechazado", "Rechazar simulación", "Pedido cancelado y stock restituido", rejected)
        check("Última entrada", "Dos pedidos simultáneos por HTTP", "Una reserva ganadora", last_ticket)
        check("Streaming real", "Consultar los tres primarios", "Un standby streaming por base", replicas)
        if args.fallos:
            check("Caída de réplica", "Detener Compras1 y comprar por Nginx", "Compras2 atiende y compra completa", failover)
            check("Reinicio con pendiente", "Parar Compras, expirar pedido, arrancar", "Worker cancela y compensa automáticamente", recovery)
        def final():
            r = api(28082, "GET", "/inventario/estado", admin)
            assert r.status_code == 200 and r.json()["consistente"]
            assert fingerprint() == before
        check("Inventario y aislamiento", "Auditar inventario y huella de staging", "Inventario consistente y mismos contenedores/arranques staging", final)
    finally:
        (out / "resultados.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
        (out / "RESUMEN.md").write_text("# HTTP distribuido\n\n| Escenario | Estado | Obtenido |\n|---|---|---|\n" + "\n".join(f"| {r['escenario']} | {r['estado']} | {r['obtenido']} |" for r in rows)+"\n")
        print("Evidencia:", out)
        print("Aprobadas:", sum(r["estado"] == "APROBADA" for r in rows))


if __name__ == "__main__":
    main()

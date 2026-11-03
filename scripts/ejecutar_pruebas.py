#!/usr/bin/env python3
"""Ejecuta las capas de pruebas de NEXUSTIX y guarda un informe clasificado.

    python3 scripts/ejecutar_pruebas.py                       # solo pruebas locales seguras
    python3 scripts/ejecutar_pruebas.py --capas unit          # solo unitarias/estáticas (sin Docker)
    # Otras capas requieren --permitir-infraestructura; revisar destino antes.

Capas:
  unit         pruebas unitarias y estáticas (SQLite temporal, dobles HTTP) + prueba Node del frontend
  postgres     integración contra un PostgreSQL 15 DESECHABLE que este script crea y elimina
               (o el indicado en NEXUSTIX_TEST_PG_ADMIN_URL); con NEXUSTIX_TEST_PG_BIN=<bin de
               PostgreSQL> se añaden la replicación real primario/standby y pg_dump/pg_restore
  resiliencia  fallos reales sobre el entorno de pruebas aislado (Docker, nexustix-auth-staging)
  staging      suites HTTP históricas de tests/staging (mismo entorno de pruebas)

Cada ejecución crea resultados/<fecha UTC>/ con JUnit XML, salida completa y
RESUMEN.md. Clasificación: APROBADA (ejecutada y verificada), FALLIDA,
BLOQUEADA (omitida por una dependencia ausente; se indica el motivo) y
PENDIENTE (capa no ejecutada en esta corrida). Nunca borra resultados previos.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = {
    "unit": ["tests/test_authentication.py", "tests/test_backup_procedure.py", "tests/test_locust_safety.py",
             "tests/test_recuperacion_automatica.py", "tests/test_eventos_inventario.py", "tests/test_migraciones.py",
             "tests/test_configuracion.py", "tests/test_operaciones.py", "tests/test_distribuido_config.py", "tests/vm", "tests/lan"],
    "postgres": ["tests/postgres"],
    "resiliencia": ["tests/resiliencia"],
    "staging": ["tests/staging"],
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_throwaway_postgres() -> tuple[str, str]:
    """PostgreSQL 15 en tmpfs, solo en 127.0.0.1, con contraseña aleatoria y etiqueta propia."""
    password = secrets.token_urlsafe(18)
    port = free_port()
    name = f"nexustix-pgtest-{uuid.uuid4().hex[:8]}"
    result = subprocess.run([
        "docker", "run", "--detach", "--name", name, "--label", "org.nexustix.purpose=pg-tests",
        "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev", "-p", f"127.0.0.1:{port}:5432",
        "-e", f"POSTGRES_PASSWORD={password}", "postgres:15-alpine",
        "-c", "max_connections=200",
    ], capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"No se pudo crear el PostgreSQL temporal: {result.stderr.strip()[-300:]}")
    container = result.stdout.strip()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True).stdout + \
            subprocess.run(["docker", "logs", container], capture_output=True, text=True).stderr
        ready = subprocess.run(["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                               capture_output=True)
        if "init process complete" in logs and ready.returncode == 0:
            return container, f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
        time.sleep(1)
    raise RuntimeError(f"El PostgreSQL temporal {name} no quedó listo; se conserva para diagnóstico")


def remove_throwaway_postgres(container: str) -> None:
    label = subprocess.run(["docker", "inspect", "--format", '{{ index .Config.Labels "org.nexustix.purpose" }}', container],
                           capture_output=True, text=True).stdout.strip()
    if label == "pg-tests":  # solo el contenedor que creó este script
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)


def run_layer(layer: str, out: Path, env: dict[str, str]) -> int:
    junit = out / f"{layer}.xml"
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA", f"--junitxml={junit}",
               *LAYERS[layer]]
    if layer != "unit":
        command.append("--allow-infrastructure-tests")
    with (out / f"{layer}.log").open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n\n")
        log.flush()
        code = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    if layer == "unit" and shutil.which("node"):
        with (out / "unit-node.log").open("w", encoding="utf-8") as log:
            node = subprocess.run(["node", "--test", "tests/staging/frontend_auth_state.test.js"], cwd=ROOT,
                                  stdout=log, stderr=subprocess.STDOUT)
        code = code or node.returncode
    return code


def classify(out: Path, layers_run: list[str]) -> str:
    rows, totals = [], {"APROBADA": 0, "FALLIDA": 0, "BLOQUEADA": 0, "PENDIENTE": 0}
    for layer in LAYERS:
        junit = out / f"{layer}.xml"
        if layer not in layers_run or not junit.exists():
            rows.append((layer, f"(capa {layer})", "PENDIENTE", "no ejecutada en esta corrida"))
            totals["PENDIENTE"] += 1
            continue
        for case in ET.parse(junit).getroot().iter("testcase"):
            name = f"{case.get('classname', '')}::{case.get('name')}"
            if case.find("failure") is not None or case.find("error") is not None:
                node = case.find("failure") if case.find("failure") is not None else case.find("error")
                status, detail = "FALLIDA", (node.get("message") or "")[:160]
            elif case.find("skipped") is not None:
                status, detail = "BLOQUEADA", (case.find("skipped").get("message") or "")[:160]
            else:
                status, detail = "APROBADA", f"{float(case.get('time', 0)):.2f} s"
            totals[status] += 1
            rows.append((layer, name, status, detail.replace("|", "/").replace("\n", " ")))
    if (out / "unit-node.log").exists():
        text = (out / "unit-node.log").read_text(encoding="utf-8", errors="replace")
        status = "APROBADA" if "# fail 0" in text or "ℹ fail 0" in text else "FALLIDA"
        totals[status] += 1
        rows.append(("unit", "node::frontend_auth_state.test.js", status, "Node --test"))
    lines = [f"# Resultados de pruebas NEXUSTIX — {out.name}", "",
             "| Estado | Total |", "|---|---|"] + [f"| {k} | {v} |" for k, v in totals.items()] + [
             "", "| Capa | Prueba | Estado | Detalle |", "|---|---|---|---|"]
    lines += [f"| {layer} | `{name}` | {status} | {detail} |" for layer, name, status, detail in rows]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capas", nargs="+", choices=list(LAYERS), default=["unit"])
    parser.add_argument("--permitir-infraestructura", action="store_true",
                        help="Opt-in para capas que crean bases o intervienen Docker/HTTP; verificar destino antes")
    parser.add_argument("--salida", type=Path, default=ROOT / "resultados")
    args = parser.parse_args()
    if any(layer != "unit" for layer in args.capas) and not args.permitir_infraestructura:
        parser.error("Las capas postgres/resiliencia/staging requieren --permitir-infraestructura")
    out = args.salida / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    exit_code = 0
    pg_container = None
    try:
        for layer in args.capas:
            layer_env = dict(env)
            if layer == "postgres":
                if env.get("NEXUSTIX_TEST_PG_ADMIN_URL"):
                    # Servidor desechable ya preparado por el operador (p. ej. binarios locales).
                    print("postgres: se usa NEXUSTIX_TEST_PG_ADMIN_URL ya definido (servidor desechable existente)")
                elif not shutil.which("docker"):
                    print("postgres: Docker no disponible; la capa queda BLOQUEADA")
                else:
                    pg_container, url = start_throwaway_postgres()
                    layer_env["NEXUSTIX_TEST_PG_ADMIN_URL"] = url
            if layer in {"resiliencia", "staging"}:
                layer_env["NEXUSTIX_STAGING_HTTP"] = "1"
                secrets_dir = Path(env.get("NEXUSTIX_STAGING_SECRETS_DIR", "~/.config/nexustix-auth-staging-secrets")).expanduser()
                layer_env["NEXUSTIX_STAGING_SECRETS_DIR"] = str(secrets_dir)
                layer_env.setdefault("NEXUSTIX_STAGING_ADMIN_EMAIL", "qa-admin@example.com")
                layer_env.setdefault("NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE", str(secrets_dir / "admin-password"))
            print(f"== Capa {layer} ...", flush=True)
            code = run_layer(layer, out, layer_env)
            print(f"   código {code} (detalle en {out / (layer + '.log')})")
            exit_code = exit_code or code
            if layer == "postgres" and pg_container:
                remove_throwaway_postgres(pg_container)
                pg_container = None
    finally:
        if pg_container:
            remove_throwaway_postgres(pg_container)
        summary = classify(out, args.capas)
        (out / "RESUMEN.md").write_bytes(summary.encode("utf-8"))  # LF también en Windows
        print(summary.split("\n\n| Capa")[0])
        print(f"Informe completo: {out / 'RESUMEN.md'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

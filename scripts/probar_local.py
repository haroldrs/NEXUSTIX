#!/usr/bin/env python3
"""Pruebas con un PostgreSQL efímero nuevo; nunca usa Docker ni bases actuales."""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", default="/usr/pgsql-18/bin")
    parser.add_argument("--permitir-infraestructura", action="store_true",
                        help="Autoriza crear y detener PostgreSQL efímero; nunca apunta a bases existentes")
    parser.add_argument("tests", nargs="*", default=["tests/postgres"])
    args = parser.parse_args()
    if not args.permitir_infraestructura:
        parser.error("Se requiere --permitir-infraestructura; consulte docs/PRUEBAS_Y_EVIDENCIAS.md")
    root = Path(tempfile.mkdtemp(prefix="nexustix-pg-local-"))
    data = root / "data"
    password = secrets.token_urlsafe(32)
    pwfile = root / "password"
    pwfile.write_text(password)
    pwfile.chmod(0o600)
    tool = lambda name: str(Path(args.pg_bin) / name)
    subprocess.run([tool("initdb"), "-D", str(data), "-U", "postgres", "-E", "UTF8", "--locale=C",
                    "--auth-host=scram-sha-256", "--auth-local=trust", "--pwfile", str(pwfile)], check=True, stdout=subprocess.DEVNULL)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "NEXUSTIX_TEST_PG_ADMIN_URL": f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres",
           "NEXUSTIX_TEST_PG_BIN": args.pg_bin}
    for name in ("DATABASE_URL", "DATABASE_URL_FILE"):
        env.pop(name, None)
    started = False
    try:
        subprocess.run([tool("pg_ctl"), "-D", str(data), "-l", str(root/"postgres.log"), "-w", "-t", "30",
                        "-o", f"-c listen_addresses=127.0.0.1 -c unix_socket_directories={root} -p {port}", "start"], check=True)
        started = True
        output = Path("resultados") / ("distribuido-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        output.mkdir(parents=True, exist_ok=True)
        with (output / "postgres.log").open("w") as log:
            result = subprocess.run([sys.executable, "-m", "pytest", "-v", "--tb=short", "--disable-warnings",
                "--allow-infrastructure-tests", f"--junitxml={output / 'postgres.xml'}", *args.tests], env=env, stdout=log, stderr=subprocess.STDOUT)
        print("Resultado:", result.returncode, "Evidencia:", output)
        return result.returncode
    finally:
        if started:
            subprocess.run([tool("pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"], check=True)
        pwfile.unlink(missing_ok=True)
        print("Clúster nuevo detenido, conservado para diagnóstico:", root)


if __name__ == "__main__":
    raise SystemExit(main())

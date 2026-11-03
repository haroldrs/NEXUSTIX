#!/usr/bin/env python3
"""Operación diaria de NEXUSTIX con el entorno SIEMPRE explícito (demo | pruebas).

    python3 scripts/nx.py demo preflight        # requisitos de Fedora/Docker, secretos y puertos
    python3 scripts/nx.py demo secretos         # genera la carpeta de secretos (una vez)
    python3 scripts/nx.py demo up               # construye imágenes y arranca esperando healthchecks
    python3 scripts/nx.py demo admin --email admin@example.com
    python3 scripts/nx.py demo status           # contenedores, health HTTP y rol de cada PostgreSQL
    python3 scripts/nx.py demo verificar        # comprobación de solo lectura (catálogo y salud)
    python3 scripts/nx.py demo auditoria --email admin@example.com   # consistencia stock/compras
    python3 scripts/nx.py demo recuperar --email admin@example.com   # /compras/recuperar-servidor
    python3 scripts/nx.py pruebas verificar --compra   # compra real de punta a punta (solo pruebas)
    python3 scripts/nx.py demo respaldo --destino ~/nexustix-backups/demo-$(date +%F)
    python3 scripts/nx.py demo stop             # detiene sin borrar contenedores ni volúmenes
    python3 scripts/nx.py pruebas reset-datos --confirmar nexustix-auth-staging
    python3 scripts/nx.py demo huella           # resumen de solo lectura (prueba de no alteración)

Ningún comando borra volúmenes: "down" se usa siempre sin la opción de volúmenes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entornos  # noqa: E402
import pg_docker  # noqa: E402

ROOT = entornos.ROOT


def ha_activo(entorno: entornos.Entorno) -> bool:
    """El overlay de replicación está en uso si existe algún contenedor *_db_replica del proyecto."""
    try:
        return any(entornos.containers(entorno, f"{db}_replica") for db in entornos.DATABASES)
    except RuntimeError:
        return False


def compose(entorno: entornos.Entorno, *args: str, ha: bool | None = None, timeout: int = 1800,
            capture: bool = False) -> subprocess.CompletedProcess:
    use_ha = ha_activo(entorno) if ha is None else ha
    command = [*entorno.compose_args(ha=use_ha), *args]
    if capture:
        return entornos.run(command, env=entorno.env(), timeout=timeout)
    return subprocess.run(command, cwd=ROOT, env=entorno.env(), timeout=timeout, check=False)


def http_json(url: str, timeout: float = 5) -> tuple[int, dict | list | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "null")
        except ValueError:
            return exc.code, None
    except (OSError, ValueError):
        return 0, None


# ---------------------------------------------------------------- preflight
def cmd_preflight(entorno: entornos.Entorno, _args) -> int:
    problems, notes = [], []

    def check(ok: bool, message: str, fix: str = "") -> None:
        (notes if ok else problems).append(("OK  " if ok else "FALTA ") + message + ("" if ok or not fix else f"  -> {fix}"))

    check(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]} (>= 3.9)")
    docker = shutil.which("docker")
    check(bool(docker), "docker en PATH", "sudo dnf install docker-ce docker-ce-cli containerd.io docker-compose-plugin")
    if docker:
        info = entornos.run(["docker", "info", "--format", "{{json .SecurityOptions}}"], timeout=30)
        check(info.returncode == 0, "acceso al daemon Docker",
              "sudo systemctl enable --now docker && sudo usermod -aG docker $USER (y volver a iniciar sesión)")
        version = entornos.run(["docker", "compose", "version", "--short"], timeout=30)
        raw = version.stdout.strip().lstrip("v")
        try:
            major, minor = (int(x) for x in raw.split(".")[:2])
            compose_ok = (major, minor) >= (2, 20)
        except ValueError:
            compose_ok = False
        check(compose_ok, f"Docker Compose v2 >= 2.20 (detectado: {raw or 'ninguno'})", "sudo dnf install docker-compose-plugin")
        if info.returncode == 0 and "selinux" in info.stdout:
            notes.append("NOTA SELinux activo en Docker: los secretos se etiquetan con container_file_t al generarlos.")
    getenforce = shutil.which("getenforce")
    if getenforce:
        mode = entornos.run([getenforce]).stdout.strip()
        notes.append(f"NOTA SELinux del sistema: {mode}")
    check(bool(shutil.which("openssl")), "openssl (claves JWT)", "sudo dnf install openssl")
    secrets_dir = entorno.secrets_dir
    present = {p.name for p in secrets_dir.iterdir()} if secrets_dir.is_dir() else set()
    missing = sorted(set(entornos.SECRET_FILES) - present)
    check(not missing, f"secretos del entorno en {secrets_dir}",
          f"python3 scripts/nx.py {entorno.nombre} secretos" if not present else f"faltan {', '.join(missing)}")
    other_dirs = {e.secrets_dir for e in entornos.ENTORNOS.values() if e.nombre != entorno.nombre}
    check(secrets_dir not in other_dirs, "carpeta de secretos distinta de la del otro entorno")
    running = bool(entornos.containers(entorno, "frontend", include_stopped=False)) if docker else False
    for service, port in entorno.puertos.items():
        if running:
            break
        with socket.socket() as probe:
            probe.settimeout(0.5)
            busy = probe.connect_ex(("127.0.0.1", port)) == 0
        check(not busy, f"puerto {port} libre ({service})", "detenga el proceso que lo usa")
    usage = shutil.disk_usage(ROOT)
    check(usage.free > 5 * 1024 ** 3, f"espacio libre {usage.free // 1024 ** 3} GiB (>= 5 GiB)")
    for line in notes + problems:
        print(line)
    print("\nRESULTADO:", "listo" if not problems else f"{len(problems)} problema(s)")
    return 0 if not problems else 1


def cmd_secretos(entorno: entornos.Entorno, args) -> int:
    import generar_secretos
    extra = ["--public-host", args.public_host] if args.public_host else []
    if args.legacy_db_password:
        extra.append("--legacy-db-password")
    if args.completar:
        extra.append("--completar")
    return generar_secretos.main(["--entorno", entorno.nombre, *extra])


# ---------------------------------------------------------------- ciclo de vida
def cmd_up(entorno: entornos.Entorno, args) -> int:
    if not entorno.secrets_dir.is_dir():
        print(f"No existe {entorno.secrets_dir}. Ejecute: python3 scripts/nx.py {entorno.nombre} secretos", file=sys.stderr)
        return 1
    up = ["up", "-d", "--wait", "--wait-timeout", str(args.timeout)]
    if not args.no_build:
        up.insert(2, "--build")
    result = compose(entorno, *up, ha=args.ha or None)
    if result.returncode:
        print("\nAlgún servicio no quedó saludable. Diagnóstico: "
              f"python3 scripts/nx.py {entorno.nombre} status  y  python3 scripts/nx.py {entorno.nombre} logs <servicio>",
              file=sys.stderr)
        return result.returncode
    return cmd_status(entorno, args)


def cmd_stop(entorno: entornos.Entorno, args) -> int:
    # stop conserva contenedores, redes y volúmenes; "start"/"up" los reanuda.
    return compose(entorno, "stop", *args.servicios).returncode


def cmd_start(entorno: entornos.Entorno, args) -> int:
    return compose(entorno, "start", *args.servicios).returncode


def cmd_down(entorno: entornos.Entorno, args) -> int:
    if args.confirmar != entorno.proyecto:
        print(f"Para eliminar los contenedores (no los volúmenes) repita con --confirmar {entorno.proyecto}", file=sys.stderr)
        return 1
    return compose(entorno, "down").returncode  # sin -v: los volúmenes y los datos se conservan


def cmd_logs(entorno: entornos.Entorno, args) -> int:
    return compose(entorno, "logs", "--tail", str(args.tail), "--timestamps", *args.servicios).returncode


def cmd_status(entorno: entornos.Entorno, _args) -> int:
    listed = compose(entorno, "ps", "--all", "--format", "json", capture=True, timeout=60)
    rows = []
    for line in listed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = json.loads(line)
        rows.extend(parsed if isinstance(parsed, list) else [parsed])
    print(f"Entorno {entorno.nombre} · proyecto Compose {entorno.proyecto} · replicación: {'sí' if ha_activo(entorno) else 'no'}")
    print(f"{'SERVICIO':22} {'ESTADO':10} {'SALUD':10} CONTENEDOR")
    for row in sorted(rows, key=lambda r: r.get("Service", "")):
        print(f"{row.get('Service', '?'):22} {row.get('State', '?'):10} {row.get('Health', '') or '-':10} {row.get('ID', '')[:12]}")
    print("\nHealth HTTP (desde el host):")
    healthy = True
    for service, port in entorno.puertos.items():
        path = "/lb-health" if service == "compras_lb" else "/health"
        code, body = http_json(f"http://127.0.0.1:{port}{path}", timeout=4)
        ok = code == 200 and isinstance(body, dict) and body.get("status") == "ok"
        healthy &= ok
        detail = body.get("db", "") if isinstance(body, dict) else ""
        print(f"  {service:12} :{port:<6} {'OK' if ok else f'FALLA (HTTP {code})'} {detail}")
    code, body = http_json(f"http://127.0.0.1:{entorno.puertos['compras_lb']}/health", timeout=6)
    print(f"  compras vía balanceador: {'OK instancia ' + str(body.get('instancia')) if code == 200 and isinstance(body, dict) else f'FALLA (HTTP {code})'}")
    print("\nPostgreSQL:")
    for base in entornos.DATABASES:
        try:
            states = [f"{n.servicio}={'standby' if n.en_recuperacion else 'primario' if n.en_recuperacion is False else 'detenido' if not n.running else '?'}"
                      for n in pg_docker.nodos(entorno, base)]
        except (RuntimeError, pg_docker.PgError) as exc:
            states = [f"error: {exc}"]
        print(f"  {base:12} {' · '.join(states) or 'sin contenedor'}")
    return 0 if healthy else 1


def cmd_admin(entorno: entornos.Entorno, args) -> int:
    print(f"Creando el administrador {args.email}; la contraseña está en {entorno.secrets_dir / 'admin-password'} (no se muestra).")
    return compose(entorno, "exec", "-T", "usuarios", "python", "bootstrap_admin.py", "--email", args.email,
                   "--password-file", "/run/secrets/admin_password", "--if-missing").returncode


def _admin_token(entorno: entornos.Entorno, email: str) -> str:
    password = (entorno.secrets_dir / "admin-password").read_text(encoding="utf-8").strip()
    request = urllib.request.Request(f"{entorno.url('usuarios')}/auth/login", method="POST",
                                     data=json.dumps({"email": email, "password": password}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())["access_token"]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"login de administrador rechazado (HTTP {exc.code}); ¿ejecutó 'nx.py {entorno.nombre} admin'?") from None


def _admin_call(entorno: entornos.Entorno, email: str, method: str, path: str) -> int:
    token = _admin_token(entorno, email)
    request = urllib.request.Request(f"{entorno.url('compras_lb')}{path}", method=method,
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            print(json.dumps(json.loads(response.read()), ensure_ascii=False, indent=2))
            return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}", file=sys.stderr)
        return 1


def cmd_auditoria(entorno: entornos.Entorno, args) -> int:
    return _admin_call(entorno, args.email, "GET", "/auditoria/consistencia")


def cmd_recuperar(entorno: entornos.Entorno, args) -> int:
    return _admin_call(entorno, args.email, "POST", "/compras/recuperar-servidor")


def cmd_verificar(entorno: entornos.Entorno, args) -> int:
    if args.compra and not entorno.datos_sinteticos and not args.permitir_compra_en_demo:
        print("La compra de verificación modifica datos. En demo exige --permitir-compra-en-demo.", file=sys.stderr)
        return 1
    command = [sys.executable, str(ROOT / "scripts" / "verificar_flujo.py"), "--entorno", entorno.nombre]
    if args.compra:
        command.append("--compra")
    return subprocess.run(command, cwd=ROOT, env=entorno.env(), check=False).returncode


# ---------------------------------------------------------------- datos
def _exec_sql(entorno: entornos.Entorno, base: str, sql: str) -> str:
    return pg_docker.psql(pg_docker.primario(entorno, base).container, base, sql)


def cmd_reset_datos(entorno: entornos.Entorno, args) -> int:
    """Vacía compras, claves idempotentes, journal y reservas, y restablece el stock base."""
    if args.confirmar != entorno.proyecto:
        print(f"Operación destructiva sobre {entorno.descripcion}. Repita con --confirmar {entorno.proyecto}", file=sys.stderr)
        return 1
    if not entorno.datos_sinteticos:
        verified = args.respaldo_verificado and (Path(args.respaldo_verificado).expanduser() / "verification.json")
        if not verified or not verified.is_file() or json.loads(verified.read_text(encoding="utf-8")).get("status") != "verified":
            print("En demo el reset exige --respaldo-verificado DIR con un respaldo verificado (nx.py demo respaldo).",
                  file=sys.stderr)
            return 1
    if _exec_sql(entorno, "compras_db", "SELECT to_regclass('public.ordenes') IS NOT NULL;").strip() == "t":
        print("Reset legacy bloqueado: esta base contiene el esquema de pedidos. Use un laboratorio nuevo; no se eliminan pedidos ni entradas.", file=sys.stderr)
        return 1
    print("Deteniendo compras1, compras2 y eventos (sin escrituras durante el reset)...")
    if compose(entorno, "stop", "compras1", "compras2", "eventos").returncode:
        return 1
    try:
        _exec_sql(entorno, "compras_db",
                  "TRUNCATE TABLE compras, purchase_idempotency, journal_compras RESTART IDENTITY;")
        _exec_sql(entorno, "eventos_db", "TRUNCATE TABLE reservas_stock; DELETE FROM eventos WHERE id NOT IN (1, 2, 3, 4, 5);")
        if args.incluir_usuarios:
            _exec_sql(entorno, "usuarios_db",
                      "DELETE FROM refresh_sessions WHERE user_id IN (SELECT id FROM usuarios WHERE role <> 'administrador');"
                      "DELETE FROM usuarios WHERE role <> 'administrador';")
        seed = compose(entorno, "run", "--rm", "--no-deps", "eventos", "python", "seed.py")
        if seed.returncode:
            return seed.returncode
    finally:
        print("Reanudando eventos, compras1 y compras2...")
        compose(entorno, "start", "eventos", "compras1", "compras2")
    print("Datos restablecidos. Compruebe con: python3 scripts/nx.py", entorno.nombre, "status")
    return 0


def cmd_respaldo(entorno: entornos.Entorno, args) -> int:
    import backup_main_databases as backup
    destino = Path(args.destino).expanduser()
    paused = ("usuarios", "eventos", "compras1", "compras2")
    print(f"Pausando escrituras ({', '.join(paused)}) para un respaldo coherente de las tres bases...")
    if compose(entorno, "stop", *paused).returncode:
        return 1
    try:
        code = backup.main(["backup", "--entorno", entorno.nombre, "--compose-project", entorno.proyecto,
                            "--output-dir", str(destino), "--confirm-writes-paused"])
    finally:
        compose(entorno, "start", *paused)
    if code or args.sin_verificar:
        return code
    print("Verificando el respaldo en un PostgreSQL temporal aislado (no toca el entorno)...")
    return backup.main(["verify", "--backup-dir", str(destino)] + (["--rehearse-migrations"] if args.ensayar_migraciones else []))


def cmd_huella(entorno: entornos.Entorno, args) -> int:
    """Resumen de solo lectura: filas por tabla, contenedores y volúmenes del entorno."""
    import backup_main_databases as backup
    report = {"entorno": entorno.nombre, "proyecto": entorno.proyecto, "bases": {}, "contenedores": {}, "volumenes": {}}
    for base in entornos.DATABASES:
        try:
            node = pg_docker.primario(entorno, base)
            schema = pg_docker.psql_json(node.container, base, backup.SCHEMA_SQL)
            report["bases"][base] = {"nodo": node.servicio, "filas": schema.get("row_counts"),
                                     "revision": schema.get("alembic_revision")}
        except (pg_docker.PgError, RuntimeError) as exc:
            report["bases"][base] = {"error": str(exc)}
    for service in entornos.BASE_SERVICES:
        report["contenedores"][service] = [c[:12] for c in entornos.containers(entorno, service)]
    listed = entornos.run(["docker", "volume", "ls", "--format", "{{.Name}}"], timeout=30).stdout.split()
    prefix = {"demo": os.environ.get("NEXUSTIX_VOLUME_PREFIX", "nexustix") + "_", "pruebas": "nexustix_auth_staging_"}[entorno.nombre]
    for volume in sorted(v for v in listed if v.startswith(prefix)):
        report["volumenes"][volume] = entornos.run(
            ["docker", "volume", "inspect", "--format", "{{.CreatedAt}}", volume], timeout=30).stdout.strip()
    output = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.salida:
        Path(args.salida).expanduser().write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("entorno", choices=sorted(entornos.ENTORNOS))
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("preflight")
    p = sub.add_parser("secretos")
    p.add_argument("--public-host")
    p.add_argument("--legacy-db-password", action="store_true")
    p.add_argument("--completar", action="store_true", help="añade solo los secretos que faltan a una carpeta existente")
    p = sub.add_parser("up")
    p.add_argument("--ha", action="store_true", help="incluir el overlay de replicación")
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--timeout", type=int, default=420)
    for name in ("stop", "start"):
        p = sub.add_parser(name)
        p.add_argument("servicios", nargs="*")
    p = sub.add_parser("down")
    p.add_argument("--confirmar", default="")
    p = sub.add_parser("logs")
    p.add_argument("servicios", nargs="*")
    p.add_argument("--tail", type=int, default=100)
    sub.add_parser("status")
    p = sub.add_parser("admin")
    p.add_argument("--email", required=True)
    default_admin = os.environ.get("NEXUSTIX_ADMIN_EMAIL", "admin@example.com")
    for name in ("auditoria", "recuperar"):
        p = sub.add_parser(name)
        p.add_argument("--email", default=default_admin, help="administrador (contraseña en <secretos>/admin-password)")
    p = sub.add_parser("verificar")
    p.add_argument("--compra", action="store_true")
    p.add_argument("--permitir-compra-en-demo", action="store_true")
    p = sub.add_parser("reset-datos")
    p.add_argument("--confirmar", default="")
    p.add_argument("--respaldo-verificado")
    p.add_argument("--incluir-usuarios", action="store_true", help="borra también compradores (conserva administradores)")
    p = sub.add_parser("respaldo")
    p.add_argument("--destino", required=True)
    p.add_argument("--sin-verificar", action="store_true")
    p.add_argument("--ensayar-migraciones", action="store_true")
    p = sub.add_parser("huella")
    p.add_argument("--salida")
    args = parser.parse_args(argv)
    entorno = entornos.get(args.entorno)
    handlers = {
        "preflight": cmd_preflight, "secretos": cmd_secretos, "up": cmd_up, "stop": cmd_stop, "start": cmd_start,
        "down": cmd_down, "logs": cmd_logs, "status": cmd_status, "admin": cmd_admin, "verificar": cmd_verificar,
        "auditoria": cmd_auditoria, "recuperar": cmd_recuperar,
        "reset-datos": cmd_reset_datos, "respaldo": cmd_respaldo, "huella": cmd_huella,
    }
    try:
        return handlers[args.comando](entorno, args)
    except (pg_docker.PgError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

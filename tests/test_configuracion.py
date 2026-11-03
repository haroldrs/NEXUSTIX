"""Comprobaciones estáticas de configuración: Compose, Nginx, frontend, scripts y secretos.

No requieren Docker. Detectan regresiones que impedirían una instalación limpia
o que mezclarían el entorno de pruebas con el de demostración.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import entornos  # noqa: E402

DB = {"usuarios_db", "eventos_db", "compras_db"}
APPS = {"usuarios", "eventos", "compras1", "compras2", "compras_lb", "frontend"}


def compose(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


@pytest.fixture(params=["docker-compose.yml", "docker-compose.staging.yml"])
def env_compose(request):
    return request.param, compose(request.param)


def test_compose_has_the_nine_services_with_healthchecks_and_restart(env_compose):
    name, data = env_compose
    services = data["services"]
    assert set(services) == DB | APPS, name
    for service, spec in services.items():
        assert "container_name" not in spec, f"{name}:{service} no debe fijar container_name"
        assert spec.get("healthcheck", {}).get("test"), f"{name}:{service} sin healthcheck"
        assert spec.get("restart"), f"{name}:{service} sin política de reinicio"


def test_apis_wait_for_healthy_database_and_lb_does_not_revive_replicas(env_compose):
    name, data = env_compose
    services = data["services"]
    for api, db in (("usuarios", "usuarios_db"), ("eventos", "eventos_db"),
                    ("compras1", "compras_db"), ("compras2", "compras_db")):
        assert services[api]["depends_on"][db]["condition"] == "service_healthy", f"{name}:{api}"
    assert "depends_on" not in services["compras_lb"], "el balanceador debe arrancar sin las réplicas"
    assert "compras1" not in services["compras2"].get("depends_on", {}), "compras2 no debe revivir compras1"
    assert services["usuarios"]["depends_on"].keys() == {"usuarios_db"}
    for replica in ("compras1", "compras2"):
        assert "usuarios" not in services[replica].get("depends_on", {}), "Compras valida el JWT localmente"


def test_images_are_built_locally_and_never_pulled_by_local_name(env_compose):
    name, data = env_compose
    for service, spec in data["services"].items():
        image = spec.get("image", "")
        if image.startswith(("nexustix/", "nexustix-staging/")):
            assert "build" in spec, f"{name}:{service} usa {image} sin build (Compose intentaría descargarla)"


def test_demo_and_test_environments_are_isolated():
    demo, test = compose("docker-compose.yml"), compose("docker-compose.staging.yml")
    assert demo["name"] == "nexustix" and test["name"] == "nexustix-auth-staging"
    assert {v["name"] for v in demo["volumes"].values()}.isdisjoint({v["name"] for v in test["volumes"].values()})
    assert all(v["name"].startswith("nexustix_auth_staging_") for v in test["volumes"].values())
    assert demo["networks"]["red_conciertos"]["name"] != test["networks"]["staging"]["name"]
    demo_images = {s["image"] for s in demo["services"].values()}
    test_images = {s["image"] for s in test["services"].values()}
    assert demo_images.isdisjoint(test_images), "construir pruebas no debe reemplazar imágenes de demo"
    demo_secret_files = {s["file"] for s in demo["secrets"].values()}
    test_secret_files = {s["file"] for s in test["secrets"].values()}
    assert all("NEXUSTIX_SECRETS_DIR" in f for f in demo_secret_files)
    assert all("NEXUSTIX_STAGING_SECRETS_DIR" in f for f in test_secret_files)
    for spec in test["services"].values():
        for port in spec.get("ports", []):
            assert port.startswith("127.0.0.1:18"), f"puerto de pruebas fuera de loopback: {port}"
    for spec in demo["services"].values():
        for port in spec.get("ports", []):
            assert port.startswith("${NEXUSTIX_BIND_ADDRESS:-127.0.0.1}:"), port


def test_compose_secrets_match_generated_files(env_compose):
    _, data = env_compose
    referenced = {Path(spec["file"]).name for spec in data["secrets"].values()}
    assert referenced <= set(entornos.SECRET_FILES)
    overlay = compose("docker-compose.replicacion.yml")
    assert {Path(spec["file"]).name for spec in overlay["secrets"].values()} <= set(entornos.SECRET_FILES)


def test_replication_overlay_adds_one_standby_per_database_and_multi_host_urls():
    overlay = compose("docker-compose.replicacion.yml")
    services = overlay["services"]
    for db in DB:
        replica = services[f"{db}_replica"]
        assert replica["environment"]["NEXUSTIX_PG_UPSTREAM_HOST"] == db
        assert "ports" not in replica, "las réplicas no se publican en el host"
        assert any(f"cluster_name={db}_replica" in arg for arg in replica["command"])
        assert "replication_password" in services[db]["secrets"]
    for api, db in (("usuarios", "usuarios_db"), ("eventos", "eventos_db"),
                    ("compras1", "compras_db"), ("compras2", "compras_db")):
        assert services[api]["environment"]["DATABASE_URL_FILE"].endswith("_database_url_ha")
        assert services[api]["depends_on"][db]["required"] is False


def test_generated_ha_url_targets_only_the_writable_node():
    import generar_secretos
    from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2
    from sqlalchemy.engine.url import make_url

    simple, ha = generar_secretos.database_urls("compras", "secreto")
    assert simple == "postgresql://postgres:secreto@compras_db:5432/compras_db"
    _, params = PGDialect_psycopg2().create_connect_args(make_url(ha))
    assert params["host"] == "compras_db,compras_db_replica"
    assert params["port"] == "5432,5432"
    assert params["target_session_attrs"] == "read-write"


def test_lb_retries_only_idempotent_purchase_endpoint():
    conf = (ROOT / "compras_lb" / "nginx.conf").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in conf.splitlines())
    assert code.count("non_idempotent") == 1
    block = code[code.index("location = /compras"):]
    block = block[:block.index("}")]
    assert "non_idempotent" in block
    assert len(re.findall(r"server compras[12]:8003 [^;]*resolve;", code)) == 2
    assert "zone compras_backend" in code and "resolver 127.0.0.11" in code
    assert "proxy_next_upstream_tries 2;" in code
    dockerfile = (ROOT / "compras_lb" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"FROM nginx:1\.(2[89]|[3-9]\d)", dockerfile), "resolve en upstream requiere nginx >= 1.27.3"


def test_frontend_registers_login_on_load_not_after_a_purchase():
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    submit = app_js[app_js.index("async function submitPurchase"):app_js.index("async function loadAdminEvents")]
    on_load = app_js[app_js.index('document.addEventListener("DOMContentLoaded"'):]
    for wiring in ('document.getElementById("auth-open").addEventListener',
                   'document.getElementById("auth-form").addEventListener("submit", submitAuth)'):
        assert wiring not in submit, "el inicio de sesión no puede depender de haber comprado"
        assert wiring in on_load
    assert "restoreSession();" in on_load
    assert not (ROOT / "frontend" / "app.staging.js").exists(), "demo y pruebas comparten un único app.js"


def test_frontend_image_serves_environment_config_with_fallback():
    conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    assert "try_files /frontend_config @config_por_defecto;" in conf
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    for asset in ("index.html", "style.css", "app.js", "config.js"):
        assert f"frontend/{asset}" in dockerfile


SCRIPT_GLOBS = ("scripts/*.py", "scripts/*.sh", "postgres/*.sh", "*.py", "tests/**/*.py")


def _project_scripts():
    files = set()
    for pattern in SCRIPT_GLOBS:
        files.update(ROOT.glob(pattern))
    return sorted(f for f in files if "legacy" not in f.parts and f.name != Path(__file__).name)


def test_no_script_deletes_volumes():
    forbidden = re.compile(r"down\s+-v|down\s+--volumes|prune\s+--volumes|volume\s+rm|volume\s+prune")
    offenders = [str(f.relative_to(ROOT)) for f in _project_scripts() if forbidden.search(f.read_text(encoding="utf-8"))]
    assert offenders == []


def test_shell_scripts_use_lf_and_valid_syntax():
    shells = sorted(ROOT.glob("postgres/*.sh")) + sorted(ROOT.glob("scripts/*.sh"))
    assert shells
    for script in shells:
        assert b"\r\n" not in script.read_bytes(), f"{script.name} tiene CRLF"
    shell = shutil.which("sh") or shutil.which("bash")
    if not shell:
        pytest.skip("sin intérprete sh para comprobar sintaxis")
    for script in shells:
        result = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_text_files_use_lf_line_endings():
    """Fedora/contenedores: CRLF rompe shebangs y scripts de shell."""
    suffixes = {".py", ".sh", ".yml", ".conf", ".js", ".html", ".css", ".ini", ".mako", ".txt", ".md", ".example"}
    offenders = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.is_file() and (p.suffix in suffixes or p.name == "Dockerfile")
                 and "__pycache__" not in p.parts and b"\r\n" in p.read_bytes()]
    assert offenders == []


def test_repository_contains_no_private_keys_or_generated_secrets():
    markers = tuple("BEGIN " + kind + "PRIVATE KEY" for kind in ("", "RSA ", "ENCRYPTED "))
    for path in ROOT.rglob("*"):
        if path.is_file() and ".git" not in path.parts and path.suffix not in {".pyc"}:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            assert not any(marker in text for marker in markers), f"clave privada en {path.relative_to(ROOT)}"
    for name in entornos.SECRET_FILES:
        assert not list(ROOT.rglob(name)), f"secreto generado dentro del repositorio: {name}"


def test_environment_definitions_cannot_collide(monkeypatch):
    demo, test = entornos.get("demo"), entornos.get("pruebas")
    assert demo.proyecto != test.proyecto
    assert set(demo.puertos.values()).isdisjoint(test.puertos.values())
    assert demo.secrets_default != test.secrets_default
    monkeypatch.setenv("NEXUSTIX_HA_VOLUME_PREFIX", "nexustix")
    monkeypatch.setenv("NEXUSTIX_PG_IMAGE", "nexustix/postgres:15")
    env = test.env()
    assert env["NEXUSTIX_HA_VOLUME_PREFIX"] == "nexustix_auth_staging", "la shell no puede redirigir pruebas a volúmenes de demo"
    assert env["NEXUSTIX_PG_IMAGE"] == "nexustix-staging/postgres:15"
    assert env["NEXUSTIX_HA_SECRETS_DIR"] == str(test.secrets_dir)
    assert test.compose_args()[:4] == ["docker", "compose", "--project-name", "nexustix-auth-staging"]


def test_secret_completion_keeps_existing_staging_secrets(tmp_path, monkeypatch):
    """Carpeta de staging de la versión anterior: se añaden los archivos nuevos sin tocar los existentes."""
    import generar_secretos

    monkeypatch.setattr(generar_secretos.shutil, "which", lambda _name: None)
    old = tmp_path / "staging-anterior"
    old.mkdir()
    previous = {f"{s}-db-password": f"clave-{s}" for s in ("usuarios", "eventos", "compras")}
    previous.update({f"{s}-database-url": f"postgresql://postgres:clave-{s}@{s}_db:5432/{s}_db"
                     for s in ("usuarios", "eventos", "compras")})
    previous.update({"jwt-private.pem": "privada-existente", "jwt-public.pem": "publica-existente",
                     "eventos-service-token": "token-existente", "frontend-config.js": "const CONFIG = {};"})
    for name, value in previous.items():
        (old / name).write_text(value)
    assert generar_secretos.main(["--entorno", "pruebas", "--directory", str(old), "--completar"]) == 0
    assert {p.name for p in old.iterdir()} == set(entornos.SECRET_FILES)
    for name, value in previous.items():
        assert (old / name).read_text() == value, f"{name} no debe cambiar"
    assert "postgres:clave-compras@" in (old / "compras-database-url-ha").read_text()
    assert generar_secretos.main(["--entorno", "pruebas", "--directory", str(old), "--completar"]) == 0  # repetible
    (old / "intruso.txt").write_text("x")
    with pytest.raises(SystemExit):
        generar_secretos.main(["--entorno", "pruebas", "--directory", str(old), "--completar"])


def test_secret_generator_creates_complete_isolated_set(tmp_path, monkeypatch):
    import generar_secretos

    monkeypatch.setattr(generar_secretos.shutil, "which", lambda _name: None)  # fuerza cryptography, sin SELinux
    target = tmp_path / "pruebas-secrets"
    assert generar_secretos.main(["--entorno", "pruebas", "--directory", str(target)]) == 0
    assert {p.name for p in target.iterdir()} == set(entornos.SECRET_FILES)
    assert "localhost:18085" in (target / "frontend-config.js").read_text(encoding="utf-8")
    assert "BEGIN " + "PRIVATE KEY" in (target / "jwt-private.pem").read_text(encoding="utf-8")
    passwords = {(target / f"{s}-db-password").read_text() for s in ("usuarios", "eventos", "compras")}
    assert len(passwords) == 3, "cada base usa una contraseña distinta"
    with pytest.raises(SystemExit):
        generar_secretos.main(["--entorno", "pruebas", "--directory", str(target)])  # nunca sobrescribe
    with pytest.raises(SystemExit):
        generar_secretos.main(["--entorno", "demo", "--directory", str(ROOT / "secretos")])  # nunca dentro del repo

"""Definición única de los entornos Docker Compose de NEXUSTIX.

Todos los scripts de operación (nx.py, db_ha.py, respaldo, pruebas de
resiliencia) resuelven el entorno desde aquí: nombre de proyecto Compose,
archivos Compose, variable con la carpeta de secretos y puertos publicados.
Así ninguna operación depende de nombres de contenedor fijos ni puede
dirigirse por accidente a otro entorno.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DATABASES = ("usuarios_db", "eventos_db", "compras_db")
APP_SERVICES = ("usuarios", "eventos", "compras1", "compras2", "compras_lb", "frontend")
BASE_SERVICES = DATABASES + APP_SERVICES

SECRET_FILES = (
    "usuarios-db-password",
    "eventos-db-password",
    "compras-db-password",
    "usuarios-database-url",
    "eventos-database-url",
    "compras-database-url",
    "usuarios-database-url-ha",
    "eventos-database-url-ha",
    "compras-database-url-ha",
    "replication-password",
    "jwt-private.pem",
    "jwt-public.pem",
    "eventos-service-token",
    "frontend-config.js",
    "admin-password",
)


@dataclass(frozen=True)
class Entorno:
    nombre: str
    proyecto: str
    compose_files: tuple[str, ...]
    secrets_env: str
    secrets_default: str
    puertos: dict[str, int]
    descripcion: str
    datos_sinteticos: bool
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def secrets_dir(self) -> Path:
        return Path(os.environ.get(self.secrets_env) or os.path.expanduser(self.secrets_default)).resolve()

    def compose_args(self, ha: bool = False) -> list[str]:
        args = ["docker", "compose", "--project-name", self.proyecto]
        for name in self.compose_files:
            args += ["-f", str(ROOT / name)]
        if ha:
            args += ["-f", str(ROOT / "docker-compose.replicacion.yml")]
        return args

    def env(self) -> dict[str, str]:
        """Entorno de proceso para docker compose con la carpeta de secretos explícita."""
        result = os.environ.copy()
        secrets_dir = str(self.secrets_dir)
        result[self.secrets_env] = secrets_dir
        # El overlay de replicación lee sus secretos de esta variable común.
        result["NEXUSTIX_HA_SECRETS_DIR"] = secrets_dir
        # Valores propios del entorno: se imponen (no se heredan de la shell)
        # para que, p. ej., las réplicas de pruebas nunca usen volúmenes de demo.
        for key, value in self.extra_env.items():
            result[key] = value
        if self.nombre == "demo":
            result["NEXUSTIX_HA_VOLUME_PREFIX"] = os.environ.get("NEXUSTIX_VOLUME_PREFIX", "nexustix")
        return result

    def url(self, servicio: str) -> str:
        return f"http://localhost:{self.puertos[servicio]}"


ENTORNOS: dict[str, Entorno] = {
    "demo": Entorno(
        nombre="demo",
        proyecto="nexustix",
        compose_files=("docker-compose.yml",),
        secrets_env="NEXUSTIX_SECRETS_DIR",
        secrets_default="~/.config/nexustix/demo-secrets",
        puertos={"frontend": 8080, "usuarios": 8001, "eventos": 8002, "compras1": 8003,
                 "compras2": 8004, "compras_lb": 8005},
        descripcion="Entorno principal de demostración (datos que se presentan)",
        datos_sinteticos=False,
        extra_env={"NEXUSTIX_PG_IMAGE": "nexustix/postgres:15", "NEXUSTIX_HA_NETWORK": "red_conciertos"},
    ),
    "pruebas": Entorno(
        nombre="pruebas",
        proyecto="nexustix-auth-staging",
        compose_files=("docker-compose.staging.yml",),
        secrets_env="NEXUSTIX_STAGING_SECRETS_DIR",
        secrets_default="~/.config/nexustix-auth-staging-secrets",
        puertos={"frontend": 18080, "usuarios": 18081, "eventos": 18082, "compras1": 18083,
                 "compras2": 18084, "compras_lb": 18085},
        descripcion="Entorno de pruebas aislado (staging) con datos sintéticos",
        datos_sinteticos=True,
        extra_env={"NEXUSTIX_PG_IMAGE": "nexustix-staging/postgres:15", "NEXUSTIX_HA_NETWORK": "staging",
                   "NEXUSTIX_HA_VOLUME_PREFIX": "nexustix_auth_staging"},
    ),
}


def get(nombre: str) -> Entorno:
    try:
        return ENTORNOS[nombre]
    except KeyError:
        raise SystemExit(f"Entorno desconocido {nombre!r}; use uno de: {', '.join(ENTORNOS)}") from None


def run(args: list[str], *, env: dict[str, str] | None = None, timeout: int = 120, check: bool = False,
        input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout,
                          check=check, input=input_text)


def containers(entorno: Entorno, servicio: str, *, include_stopped: bool = True) -> list[str]:
    """IDs de contenedor del servicio, filtrados por etiquetas Compose del proyecto exacto."""
    args = ["docker", "ps", "--no-trunc", "--format", "{{.ID}}",
            "--filter", f"label=com.docker.compose.project={entorno.proyecto}",
            "--filter", f"label=com.docker.compose.service={servicio}",
            "--filter", "label=com.docker.compose.oneoff=False"]
    if include_stopped:
        args.insert(2, "--all")
    result = run(args, timeout=30)
    if result.returncode:
        raise RuntimeError(f"docker ps falló: {result.stderr.strip()[-300:]}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def container_id(entorno: Entorno, servicio: str) -> str:
    """ID del único contenedor del servicio; falla si hay cero o varios."""
    ids = containers(entorno, servicio)
    if len(ids) != 1:
        raise RuntimeError(
            f"Se esperaba exactamente un contenedor de {servicio} en el proyecto {entorno.proyecto}; hay {len(ids)}"
        )
    return ids[0]


def inspect(container: str, template: str) -> str:
    result = run(["docker", "inspect", "--format", template, container], timeout=30)
    if result.returncode:
        raise RuntimeError(f"docker inspect falló: {result.stderr.strip()[-300:]}")
    return result.stdout.strip()


def verify_labels(entorno: Entorno, container: str, servicio: str) -> None:
    """Segunda comprobación antes de una acción destructiva sobre un contenedor."""
    project = inspect(container, '{{ index .Config.Labels "com.docker.compose.project" }}')
    service = inspect(container, '{{ index .Config.Labels "com.docker.compose.service" }}')
    if project != entorno.proyecto or service != servicio:
        raise RuntimeError(
            f"Contenedor rechazado: etiquetas {project}/{service}, se esperaba {entorno.proyecto}/{servicio}"
        )

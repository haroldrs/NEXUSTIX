"""Authenticated purchase load profile. Refuses every target except staging loopback.

Preparación de datos sintéticos (evento con stock controlado y comprador QA):
    python3 scripts/preparar_carga.py --stock 300
    source ~/.config/nexustix-locust.env
    locust -f locustfile.py --host http://localhost:18085 --headless -u 20 -r 5 -t 2m --csv resultados/locust

Al terminar imprime respuestas por código, compras por réplica y, si se
definen NEXUSTIX_LOCUST_ADMIN_EMAIL y NEXUSTIX_LOCUST_ADMIN_PASSWORD_FILE, la
fila de auditoría del evento sintético (stock inicial = actual + vendidas).
"""

import collections
import os
import threading
from pathlib import Path
from uuid import uuid4

import requests
from locust import HttpUser, between, events, task
from locust.exception import StopUser

from scripts.locust_safety import load_settings

_LOCK = threading.Lock()
RESPUESTAS = collections.Counter()
REPLICAS = collections.Counter()


def _record(status: int, replica: str | None) -> None:
    with _LOCK:
        RESPUESTAS[status] += 1
        if replica:
            REPLICAS[replica] += 1


class CompradorStaging(HttpUser):
    """Synthetic buyer traffic for the isolated staging Compose project only."""

    wait_time = between(0.2, 0.6)

    def on_start(self):
        try:
            self.settings = load_settings(self.host, os.environ)
        except ValueError as exc:
            # The exception names missing settings/invalid target only; it never
            # includes credentials or their values.
            self.environment.runner.quit()
            raise StopUser(str(exc)) from None

        response = self.client.post(
            f"{self.settings.users_url}/auth/login",
            json={"email": self.settings.email, "password": self.settings.password},
            name="POST /auth/login",
        )
        if response.status_code != 200:
            self.environment.runner.quit()
            raise StopUser(f"login staging rechazado (HTTP {response.status_code})")
        try:
            payload = response.json()
            user = payload["user"]
            token = payload["access_token"]
        except (ValueError, KeyError, TypeError):
            self.environment.runner.quit()
            raise StopUser("respuesta de login staging inválida") from None
        if user.get("role") != "comprador" or not isinstance(token, str) or not token:
            self.environment.runner.quit()
            raise StopUser("la cuenta de carga debe ser un comprador sintético válido")
        self.access_token = token
        self.frontend_origin = "http://localhost:18080"

    def _refresh_access_token(self) -> bool:
        response = self.client.post(
            f"{self.settings.users_url}/auth/refresh",
            headers={"Origin": self.frontend_origin, "X-CSRF-Protection": "1"},
            name="POST /auth/refresh",
        )
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
            token = payload["access_token"]
            if payload.get("user", {}).get("role") != "comprador" or not isinstance(token, str):
                return False
        except (ValueError, KeyError, TypeError):
            return False
        self.access_token = token
        return True

    @task
    def buy_one_ticket(self):
        key = f"locust-{uuid4().hex}"
        body = {"evento_id": self.settings.event_id, "cantidad": 1}
        headers = {"Authorization": f"Bearer {self.access_token}", "Idempotency-Key": key}
        response = self.client.post(
            "/compras", headers=headers, json=body, name="POST /compras", catch_response=True,
        )
        if response.status_code == 401 and self._refresh_access_token():
            # Retry only the same logical operation with the same user, payload,
            # and key. The database idempotency constraint is the deduplication boundary.
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = self.client.post(
                "/compras", headers=headers, json=body,
                name="POST /compras (retry same key)", catch_response=True,
            )

        with response:
            replica = None
            if response.status_code == 200:
                try:
                    replica = response.json().get("atendido_por")
                except ValueError:
                    replica = None
                response.success()
            elif response.status_code == 409:
                # Expected business rejection when synthetic stock is exhausted;
                # keep it visible as a distinct Locust failure/status.
                response.failure("business rejection: stock unavailable (409)")
            else:
                response.failure(f"purchase request failed (HTTP {response.status_code})")
            _record(response.status_code, replica)


@events.test_stop.add_listener
def _resumen_consistencia(environment, **_kwargs):
    print("\n== Resumen NEXUSTIX ==")
    print(f"Respuestas POST /compras por código: {dict(RESPUESTAS)}")
    print(f"Compras atendidas por réplica: {dict(REPLICAS)}")
    email = os.getenv("NEXUSTIX_LOCUST_ADMIN_EMAIL")
    password_file = os.getenv("NEXUSTIX_LOCUST_ADMIN_PASSWORD_FILE")
    try:
        settings = load_settings(environment.host or "", os.environ)
    except ValueError:
        return
    if not email or not password_file:
        print("Defina NEXUSTIX_LOCUST_ADMIN_EMAIL y NEXUSTIX_LOCUST_ADMIN_PASSWORD_FILE para auditar el stock.")
        return
    password = Path(password_file).expanduser().read_text(encoding="utf-8").strip()
    login = requests.post(f"{settings.users_url}/auth/login", json={"email": email, "password": password}, timeout=10)
    if login.status_code != 200:
        print(f"No se pudo iniciar sesión como administrador (HTTP {login.status_code})")
        return
    audit = requests.get(f"{environment.host.rstrip('/')}/auditoria/consistencia",
                         headers={"Authorization": f"Bearer {login.json()['access_token']}"}, timeout=30)
    if audit.status_code != 200:
        print(f"Auditoría no disponible (HTTP {audit.status_code})")
        return
    report = audit.json()
    row = next((e for e in report["reporte_por_evento"] if e["evento_id"] == settings.event_id), None)
    print(f"Evento sintético {settings.event_id}: {row}")
    print(f"Reservas pendientes: {len(report['reservas_pendientes'])} · todo_consistente={report['todo_consistente']}")
    if row is not None and RESPUESTAS.get(200, 0) and row["entradas_vendidas"] < RESPUESTAS[200]:
        print("ATENCIÓN: hay menos compras persistidas que respuestas 200 registradas por Locust.")

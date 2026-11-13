"""Coordinación distribuida visible en NEXUSTIX.

Cada instancia de API (Usuarios, Eventos, Compras, en cualquier computadora)
ejecuta este módulo. Cubre los temas de la Unidad II del curso:

* Servicio de nombres: directorio replicado. Cada instancia se registra con su
  nombre, servicio, nodo y dirección, y lo renueva con un latido. Cualquier
  instancia resuelve "servicio -> direcciones vivas" con un TTL para caché.
* Detección de fallos por latido (heartbeat): intervalo T y plazo T + Δ.
* Elección de líder con el algoritmo del matón (Bully): gana la prioridad mayor.
* Relojes lógicos: Lamport y vectorial, propagados en una cabecera HTTP.
* Relojes físicos: algoritmo de Cristian contra el coordinador y algoritmo de
  Berkeley dirigido por el coordinador (ajuste lógico; no cambia la hora del SO).

La lógica vive en `Coordinator`, que recibe un transporte. En producción el
transporte es HTTP; en las pruebas es un enrutador en memoria, lo que permite
simular caídas y particiones sin red.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

HEARTBEAT_S = float(os.getenv("NEXUSTIX_HEARTBEAT_SECONDS", "2"))
SUSPECT_S = float(os.getenv("NEXUSTIX_HEARTBEAT_TIMEOUT_SECONDS", "6"))   # T + Δ
DEAD_S = float(os.getenv("NEXUSTIX_HEARTBEAT_DEAD_SECONDS", "15"))
RESOLVE_TTL_S = 5
CLOCK_HEADER = "X-Nexustix-Reloj"


def utc_iso(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, timezone.utc).isoformat(timespec="milliseconds")


def compare_vectors(a: dict, b: dict) -> str:
    """Relación causal entre dos relojes vectoriales.

    'antes'      -> a ocurrió antes que b (a -> b)
    'despues'    -> b ocurrió antes que a
    'igual'      -> mismo estado
    'concurrente'-> ninguno precede al otro (a || b)
    """
    keys = set(a) | set(b)
    less = any(a.get(k, 0) < b.get(k, 0) for k in keys)
    more = any(a.get(k, 0) > b.get(k, 0) for k in keys)
    if not less and not more:
        return "igual"
    if less and not more:
        return "antes"
    if more and not less:
        return "despues"
    return "concurrente"


class Coordinator:
    def __init__(self, identity: dict, peers: list[dict], transport, clock=time.time, monotonic=time.monotonic):
        self.me = {"id": identity["id"], "servicio": identity.get("servicio", ""), "nodo": identity.get("nodo", ""),
                   "url": identity.get("url", ""), "prioridad": int(identity.get("prioridad", 0))}
        self.peers = {p["id"]: {**p, "prioridad": int(p.get("prioridad", 0))} for p in peers if p.get("id") != self.me["id"]}
        self.transport = transport
        self.clock = clock
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self.directory = {}              # id -> datos del último latido recibido
        self.coordinator = None
        self.electing = False
        self.silenced = False            # Caída simulada desde el panel.
        self.lamport = 0
        self.vector = {self.me["id"]: 0}
        self.events = deque(maxlen=200)
        self.history = deque(maxlen=60)
        self.cristian = {"desfase_ms": None, "rtt_ms": None, "referencia": None, "medido_en": None}
        self.berkeley = {"ajuste_ms": None, "ronda": None, "promedio_ms": None, "participantes": []}
        self.event_seq = 0

    # ---------------- Relojes lógicos ----------------
    def _tick(self, received=None):
        with self.lock:
            if received:
                self.lamport = max(self.lamport, int(received.get("l", 0)))
                for key, value in (received.get("v") or {}).items():
                    if isinstance(value, int) and isinstance(key, str) and len(self.vector) < 64:
                        self.vector[key] = max(self.vector.get(key, 0), value)
            self.lamport += 1
            self.vector[self.me["id"]] = self.vector.get(self.me["id"], 0) + 1
            return self.lamport, dict(self.vector)

    def record(self, event: str, received=None, correlation=None, kind="local"):
        lamport, vector = self._tick(received)
        with self.lock:
            self.event_seq += 1
            entry = {"id": f"{self.me['id']}#{self.event_seq}", "instancia": self.me["id"], "nodo": self.me["nodo"],
                     "evento": str(event)[:120], "tipo": kind, "lamport": lamport, "vector": vector,
                     "fisico": utc_iso(self.clock()), "correlacion": correlation}
            self.events.append(entry)
        return entry

    def outgoing(self, event: str, correlation=None):
        """Evento de envío: devuelve la cabecera que viaja con el mensaje."""
        entry = self.record(event, correlation=correlation, kind="envio")
        return {CLOCK_HEADER: json.dumps({"l": entry["lamport"], "v": entry["vector"]}, separators=(",", ":"))}

    def incoming(self, header_value: str | None, event: str, correlation=None):
        if not header_value:
            return None
        try:
            received = json.loads(header_value)
        except ValueError:
            return None
        if not isinstance(received, dict):
            return None
        return self.record(event, received=received, correlation=correlation, kind="recepcion")

    # ---------------- Directorio y latidos ----------------
    def heartbeat_payload(self):
        with self.lock:
            return {**self.me, "coordinador": self.coordinator, "lamport": self.lamport, "enviado": self.clock()}

    def receive_heartbeat(self, payload: dict):
        if self.silenced:
            return False
        sender = payload.get("id")
        if sender not in self.peers:
            return False  # Solo se aceptan miembros declarados en el inventario.
        with self.lock:
            previous = self.directory.get(sender)
            self.directory[sender] = {**self.peers[sender], "url": self.peers[sender].get("url"),
                                      "ultimo_latido": self.monotonic(), "ultimo_latido_utc": utc_iso(self.clock()),
                                      "coordinador_que_ve": payload.get("coordinador"), "lamport": payload.get("lamport")}
            if previous is None or self._age(previous) > SUSPECT_S:
                self._log(f"{sender} se registró en el directorio" if previous is None else f"{sender} volvió a latir")
        # Un nodo con más prioridad que el coordinador actual reaparece: Bully
        # indica que él mismo convocará una elección; aquí solo se registra.
        return True

    def _age(self, entry):
        return self.monotonic() - entry["ultimo_latido"]

    def state_of(self, peer_id):
        if peer_id == self.me["id"]:
            return "silenciada" if self.silenced else "viva"
        entry = self.directory.get(peer_id)
        if not entry:
            return "sin_registro"
        age = self._age(entry)
        return "viva" if age <= SUSPECT_S else "sospechosa" if age <= DEAD_S else "caida"

    def alive(self, peer_id):
        return self.state_of(peer_id) == "viva"

    def send_heartbeats(self):
        if self.silenced:
            return
        payload = self.heartbeat_payload()
        for peer in list(self.peers.values()):
            self.transport.post(peer, "/coordinacion/latido", payload)

    def resolve(self, service: str):
        """Servicio de nombres: direcciones vivas de un servicio, con TTL para caché."""
        with self.lock:
            rows = [self.me] if self.me["servicio"] == service and not self.silenced else []
            rows += [p for p in self.peers.values() if p.get("servicio") == service and self.alive(p["id"])]
        return {"servicio": service, "ttl_s": RESOLVE_TTL_S,
                "direcciones": [{"instancia": r["id"], "nodo": r.get("nodo"), "url": r.get("url")} for r in rows]}

    # ---------------- Bully ----------------
    def _log(self, text):
        self.history.appendleft({"momento": utc_iso(self.clock()), "lamport": self.lamport, "detalle": text})

    def start_election(self, reason="inicio"):
        if self.silenced:
            return None
        with self.lock:
            if self.electing:
                return self.coordinator
            self.electing = True
        try:
            self._log(f"{self.me['id']} convoca elección ({reason})")
            higher = [p for p in self.peers.values() if p["prioridad"] > self.me["prioridad"]]
            answered = False
            for peer in sorted(higher, key=lambda p: -p["prioridad"]):
                reply = self.transport.post(peer, "/coordinacion/eleccion", {"id": self.me["id"], "prioridad": self.me["prioridad"]})
                if reply and reply.get("ok"):
                    answered = True
                    self._log(f"{peer['id']} respondió OK: tiene mayor prioridad y continúa la elección")
                    break
            if answered:
                return None  # Espera el anuncio del ganador.
            self._become_coordinator()
            return self.me["id"]
        finally:
            with self.lock:
                self.electing = False

    def _become_coordinator(self):
        with self.lock:
            self.coordinator = self.me["id"]
        self._log(f"{self.me['id']} se proclama coordinador (nadie con mayor prioridad respondió)")
        self.record("anuncio de coordinador", kind="eleccion")
        for peer in list(self.peers.values()):
            self.transport.post(peer, "/coordinacion/coordinador", {"id": self.me["id"], "prioridad": self.me["prioridad"]})

    def receive_election(self, payload):
        """Mensaje ELECCIÓN de un nodo de menor prioridad: responder OK y competir."""
        if self.silenced:
            return None
        if int(payload.get("prioridad", -1)) < self.me["prioridad"]:
            self._log(f"Recibe ELECCIÓN de {payload.get('id')}; responde OK")
            return {"ok": True}
        return {"ok": False}

    def receive_coordinator(self, payload):
        if self.silenced:
            return False
        sender = payload.get("id")
        if sender not in self.peers and sender != self.me["id"]:
            return False
        with self.lock:
            changed = self.coordinator != sender
            self.coordinator = sender
        if changed:
            self._log(f"Nuevo coordinador anunciado: {sender}")
        return True

    def check_coordinator(self):
        """Paso periódico: si el coordinador dejó de latir, convocar elección.
        Si este nodo tiene más prioridad que el coordinador vivo, lo desplaza."""
        if self.silenced:
            return
        current = self.coordinator
        if current is None:
            self.start_election("sin coordinador")
        elif current != self.me["id"] and not self.alive(current):
            self._log(f"El coordinador {current} no late hace más de {SUSPECT_S:.0f} s")
            self.start_election("coordinador caído")
        elif current != self.me["id"] and self.peers.get(current, {}).get("prioridad", 0) < self.me["prioridad"]:
            self.start_election("nodo de mayor prioridad disponible")
        elif current == self.me["id"] and any(p["prioridad"] > self.me["prioridad"] and self.alive(p["id"]) for p in self.peers.values()):
            # Tras una partición: hay un nodo de mayor prioridad alcanzable.
            self.start_election("se reunió con un nodo de mayor prioridad")

    # ---------------- Relojes físicos ----------------
    def time_reply(self):
        return {"instancia": self.me["id"], "hora": self.clock()}

    def cristian_sync(self):
        """t0 -> pide la hora -> t1. Hora estimada = hora del servidor + RTT/2."""
        reference = self.coordinator
        if not reference or reference == self.me["id"] or self.silenced:
            self.cristian = {"desfase_ms": 0.0 if reference == self.me["id"] else None, "rtt_ms": 0.0 if reference == self.me["id"] else None,
                             "referencia": reference, "medido_en": utc_iso(self.clock())}
            return self.cristian
        peer = self.peers.get(reference)
        t0 = self.clock()
        reply = self.transport.get(peer, "/coordinacion/tiempo") if peer else None
        t1 = self.clock()
        if not reply or "hora" not in reply:
            return self.cristian
        rtt = t1 - t0
        estimated = float(reply["hora"]) + rtt / 2
        self.cristian = {"desfase_ms": round((estimated - t1) * 1000, 2), "rtt_ms": round(rtt * 1000, 2),
                         "referencia": reference, "medido_en": utc_iso(t1)}
        return self.cristian

    def berkeley_round(self):
        """Solo el coordinador: sondea relojes, promedia y envía a cada uno su ajuste."""
        if self.coordinator != self.me["id"] or self.silenced:
            return None
        readings = {self.me["id"]: 0.0}
        for peer in list(self.peers.values()):
            if not self.alive(peer["id"]):
                continue
            t0 = self.clock()
            reply = self.transport.get(peer, "/coordinacion/tiempo")
            t1 = self.clock()
            if reply and "hora" in reply:
                readings[peer["id"]] = (float(reply["hora"]) + (t1 - t0) / 2) - t1
        # Se descartan lecturas muy alejadas (relojes defectuosos) antes de promediar.
        valid = {k: v for k, v in readings.items() if abs(v) < 5}
        average = sum(valid.values()) / len(valid)
        adjustments = {k: round((average - v) * 1000, 2) for k, v in readings.items()}
        for peer_id, adjustment in adjustments.items():
            if peer_id != self.me["id"]:
                self.transport.post(self.peers[peer_id], "/coordinacion/ajuste", {"ajuste_ms": adjustment, "coordinador": self.me["id"]})
        self.berkeley = {"ajuste_ms": adjustments[self.me["id"]], "ronda": utc_iso(self.clock()),
                         "promedio_ms": round(average * 1000, 2),
                         "participantes": [{"instancia": k, "desfase_ms": round(v * 1000, 2), "ajuste_ms": adjustments[k]} for k, v in readings.items()]}
        return self.berkeley

    def receive_adjustment(self, payload):
        if self.silenced:
            return False
        with self.lock:
            self.berkeley = {**self.berkeley, "ajuste_ms": payload.get("ajuste_ms"), "ronda": utc_iso(self.clock()),
                             "coordinador": payload.get("coordinador")}
        return True

    # ---------------- Control desde el panel ----------------
    def silence(self):
        self.silenced = True
        self._log(f"{self.me['id']} simula una caída: deja de latir y de responder elecciones")

    def restore(self):
        self.silenced = False
        self._log(f"{self.me['id']} se restablece y convoca elección (Bully)")
        self.send_heartbeats()
        self.start_election("instancia restablecida")

    def tick(self):
        """Un ciclo completo; el hilo de fondo lo repite cada HEARTBEAT_S."""
        self.send_heartbeats()
        self.check_coordinator()

    def snapshot(self):
        with self.lock:
            rows = [{**self.me, "estado": self.state_of(self.me["id"]), "ultimo_latido_hace_s": 0.0, "es_yo": True}]
            for peer in sorted(self.peers.values(), key=lambda p: -p["prioridad"]):
                entry = self.directory.get(peer["id"])
                rows.append({**{k: peer.get(k) for k in ("id", "servicio", "nodo", "url", "prioridad")},
                             "estado": self.state_of(peer["id"]), "es_yo": False,
                             "ultimo_latido_hace_s": round(self._age(entry), 1) if entry else None,
                             "ultimo_latido_utc": entry.get("ultimo_latido_utc") if entry else None})
            return {"instancia": self.me, "silenciada": self.silenced, "coordinador": self.coordinator,
                    "soy_coordinador": self.coordinator == self.me["id"],
                    "parametros": {"latido_s": HEARTBEAT_S, "sospecha_s": SUSPECT_S, "caida_s": DEAD_S, "ttl_resolucion_s": RESOLVE_TTL_S},
                    "directorio": rows, "elecciones": list(self.history),
                    "reloj": {"lamport": self.lamport, "vector": dict(self.vector)},
                    "cristian": dict(self.cristian), "berkeley": dict(self.berkeley)}


class HttpTransport:
    def __init__(self, timeout=(1, 1.5)):
        import requests
        self.session = requests.Session()
        self.session.trust_env = False
        self.timeout = timeout

    def _url(self, peer, path):
        return peer.get("url", "").rstrip("/") + path

    def post(self, peer, path, payload):
        try:
            response = self.session.post(self._url(peer, path), json=payload, timeout=self.timeout, allow_redirects=False)
            return response.json() if response.status_code == 200 else None
        except Exception:
            return None

    def get(self, peer, path):
        try:
            response = self.session.get(self._url(peer, path), timeout=self.timeout, allow_redirects=False)
            return response.json() if response.status_code == 200 else None
        except Exception:
            return None


def load_peers():
    try:
        peers = json.loads(os.getenv("NEXUSTIX_PEERS") or "[]")
    except ValueError:
        return []
    return [p for p in peers if isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("url"), str)][:64]


COORDINATOR: Coordinator | None = None


def current():
    return COORDINATOR


def install(app, service: str, require_admin):
    """Registra las rutas /coordinacion/* y, si está habilitado, el hilo de latidos."""
    global COORDINATOR
    from fastapi import Depends, HTTPException, Request

    peers = load_peers()
    instance = os.getenv("NEXUSTIX_COORDINATION_ID") or os.getenv("INSTANCE_ID") or service
    me = next((p for p in peers if p["id"] == instance), None) or {"id": instance, "servicio": service,
                                                                     "nodo": os.getenv("NEXUSTIX_NODE_ID", "local"), "url": "", "prioridad": 0}
    COORDINATOR = coordinator = Coordinator({**me, "servicio": me.get("servicio") or service}, peers, HttpTransport())

    @app.middleware("http")
    async def clock_middleware(request: Request, call_next):
        header = request.headers.get(CLOCK_HEADER)
        if header:
            coordinator.incoming(header, f"recibe {request.method} {request.url.path}", request.headers.get("X-Correlation-ID"))
        return await call_next(request)

    def internal_guard():
        if coordinator.silenced:
            raise HTTPException(503, "Instancia en caída simulada")

    @app.post("/coordinacion/latido")
    def heartbeat(payload: dict):
        internal_guard()
        return {"ok": coordinator.receive_heartbeat(payload)}

    @app.post("/coordinacion/eleccion")
    def election(payload: dict):
        internal_guard()
        reply = coordinator.receive_election(payload)
        if reply and reply.get("ok"):
            threading.Thread(target=coordinator.start_election, args=("respuesta a una elección",), daemon=True).start()
        return reply or {"ok": False}

    @app.post("/coordinacion/coordinador")
    def announce(payload: dict):
        internal_guard()
        return {"ok": coordinator.receive_coordinator(payload)}

    @app.get("/coordinacion/tiempo")
    def time_now():
        internal_guard()
        return coordinator.time_reply()

    @app.post("/coordinacion/ajuste")
    def adjustment(payload: dict):
        internal_guard()
        return {"ok": coordinator.receive_adjustment(payload)}

    @app.get("/coordinacion/resolver/{servicio}")
    def resolve(servicio: str):
        internal_guard()
        return coordinator.resolve(servicio)

    @app.get("/coordinacion/eventos")
    def events():
        internal_guard()
        return {"instancia": coordinator.me["id"], "eventos": list(coordinator.events)}

    @app.get("/admin/coordinacion")
    def admin_state(_admin=Depends(require_admin)):
        return coordinator.snapshot()

    @app.get("/admin/coordinacion/eventos")
    def admin_events(_admin=Depends(require_admin)):
        """Reúne los eventos de todas las instancias vivas para comparar relojes."""
        rows = list(coordinator.events)
        reached = [coordinator.me["id"]]
        for peer in list(coordinator.peers.values()):
            if coordinator.alive(peer["id"]):
                reply = coordinator.transport.get(peer, "/coordinacion/eventos")
                if reply and isinstance(reply.get("eventos"), list):
                    rows.extend(reply["eventos"][-100:])
                    reached.append(peer["id"])
        rows.sort(key=lambda e: (e.get("lamport", 0), e.get("instancia", "")))
        return {"consultadas": reached, "eventos": rows[-300:]}

    @app.get("/admin/coordinacion/comparar")
    def admin_compare(a: str, b: str, _admin=Depends(require_admin)):
        try:
            va, vb = json.loads(a), json.loads(b)
        except ValueError:
            raise HTTPException(422, "Vectores inválidos")
        if not isinstance(va, dict) or not isinstance(vb, dict):
            raise HTTPException(422, "Vectores inválidos")
        return {"relacion": compare_vectors(va, vb)}

    def forward(instancia, path, authorization):
        """El balanceador puede entregar la orden a otra instancia: se reenvía a
        la indicada con la misma credencial de administrador."""
        peer = coordinator.peers.get(instancia)
        if not peer:
            raise HTTPException(404, "Instancia no declarada en el directorio")
        try:
            response = coordinator.transport.session.post(peer["url"].rstrip("/") + path, headers={"Authorization": authorization or ""},
                                                          timeout=(1, 3), allow_redirects=False)
        except Exception:
            raise HTTPException(503, f"No se pudo contactar a {instancia}")
        if response.status_code != 200:
            raise HTTPException(response.status_code if response.status_code < 500 else 503, f"{instancia} no aceptó la orden")
        return response.json()

    @app.post("/admin/coordinacion/simular-caida")
    def admin_silence(instancia: str | None = None, request: Request = None, _admin=Depends(require_admin)):
        if instancia and instancia != coordinator.me["id"]:
            return forward(instancia, "/admin/coordinacion/simular-caida", request.headers.get("Authorization"))
        coordinator.silence()
        return coordinator.snapshot()

    @app.post("/admin/coordinacion/restaurar")
    def admin_restore(instancia: str | None = None, request: Request = None, _admin=Depends(require_admin)):
        if instancia and instancia != coordinator.me["id"]:
            return forward(instancia, "/admin/coordinacion/restaurar", request.headers.get("Authorization"))
        coordinator.restore()
        return coordinator.snapshot()

    @app.post("/admin/coordinacion/eleccion")
    def admin_election(_admin=Depends(require_admin)):
        coordinator.start_election("solicitada por el administrador")
        return coordinator.snapshot()

    @app.post("/admin/coordinacion/sincronizar")
    def admin_sync(_admin=Depends(require_admin)):
        coordinator.cristian_sync()
        coordinator.berkeley_round()
        return coordinator.snapshot()

    if os.getenv("NEXUSTIX_COORDINATION", "1") == "1" and peers:
        def loop():
            cycles = 0
            time.sleep(1)
            while True:
                try:
                    coordinator.tick()
                    if cycles % 5 == 0:
                        coordinator.cristian_sync()
                        coordinator.berkeley_round()
                except Exception:
                    pass
                cycles += 1
                time.sleep(HEARTBEAT_S)
        threading.Thread(target=loop, name="nexustix-coordinacion", daemon=True).start()
    return coordinator

"""Correlación HTTP y logs sin cuerpos, parámetros, credenciales ni QR."""
import contextvars
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

correlation_id = contextvars.ContextVar("correlation_id", default="")
logger = logging.getLogger("nexustix.http")


def install(app, service):
    lock = Lock()
    started = datetime.now(timezone.utc).isoformat()
    counters = {"solicitudes": 0, "exitosas": 0, "errores_http": 0,
                "latencia_total_ms": 0.0, "latencia_max_ms": 0.0}
    recent = deque(maxlen=80)

    def snapshot():
        with lock:
            values = dict(counters)
            values["latencia_media_ms"] = round(values.pop("latencia_total_ms") / max(1, values["solicitudes"]), 2)
            return {"disponible": True, "servicio": service,
                    "instancia": os.getenv("INSTANCE_ID", service), "desde": started,
                    "alcance": "proceso actual; se reinicia al reiniciar la instancia",
                    **values, "recientes": list(recent)}

    app.state.http_metrics_snapshot = snapshot

    @app.middleware("http")
    async def trace(request, call_next):
        supplied = request.headers.get("X-Correlation-ID", "")
        cid = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else uuid4().hex
        token = correlation_id.set(cid)
        start = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = cid
            response.headers["X-Instance-ID"] = os.getenv("INSTANCE_ID", service)
            return response
        finally:
            route = request.scope.get("route")
            duration = round((time.monotonic() - start) * 1000, 2)
            entry = {"momento": datetime.now(timezone.utc).isoformat(),
                     "correlation_id": cid, "metodo": request.method,
                     "ruta": getattr(route, "path", "unmatched"), "status": status,
                     "latencia_ms": duration}
            with lock:
                counters["solicitudes"] += 1
                counters["exitosas" if status < 400 else "errores_http"] += 1
                counters["latencia_total_ms"] += duration
                counters["latencia_max_ms"] = max(counters["latencia_max_ms"], duration)
                recent.append(entry)
            logger.warning(json.dumps({"service": service, "instance": os.getenv("INSTANCE_ID", service),
                "correlation_id": cid, "method": request.method,
                "route": getattr(route, "path", "unmatched"), "status": status,
                "duration_ms": duration}))
            correlation_id.reset(token)

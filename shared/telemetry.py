"""Correlación HTTP y logs sin cuerpos, parámetros, credenciales ni QR."""
import contextvars
import json
import logging
import os
import re
import time
from uuid import uuid4

correlation_id = contextvars.ContextVar("correlation_id", default="")
logger = logging.getLogger("nexustix.http")


def install(app, service):
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
            logger.warning(json.dumps({"service": service, "instance": os.getenv("INSTANCE_ID", service),
                "correlation_id": cid, "method": request.method,
                "route": getattr(route, "path", "unmatched"), "status": status,
                "duration_ms": round((time.monotonic()-start)*1000, 2)}))
            correlation_id.reset(token)

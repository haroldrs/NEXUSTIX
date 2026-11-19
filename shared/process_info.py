"""Datos del proceso que atiende, leídos de /proc (Linux) sin dependencias.

Sirven para la Unidad I (procesos): PID, estado, hilos, memoria, tiempo de CPU
y cambios de contexto que hizo el planificador. Dentro de un contenedor el
servicio suele ser el PID 1: cada contenedor tiene su propio espacio de PID.
"""

from __future__ import annotations

import os
import time

STATES = {"R": "ejecutando", "S": "esperando (bloqueado)", "D": "esperando E/S", "T": "detenido", "Z": "zombi", "I": "inactivo"}


def snapshot() -> dict:
    try:
        status = {}
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                status[key] = value.strip()
        with open("/proc/self/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
        state = status.get("State", "?")[:1]
        start_ticks = int(fields[19])
        return {
            "pid": os.getpid(), "ppid": os.getppid(),
            "estado_proceso": f"{state} · {STATES.get(state, 'desconocido')}",
            "hilos": int(status.get("Threads", "0")),
            "memoria_mb": round(int(status.get("VmRSS", "0 kB").split()[0]) / 1024, 1),
            "cpu_segundos": round((int(fields[11]) + int(fields[12])) / ticks, 2),
            "proceso_activo_s": round(uptime - start_ticks / ticks),
            "cambios_contexto_voluntarios": int(status.get("voluntary_ctxt_switches", "0")),
            "cambios_contexto_forzados": int(status.get("nonvoluntary_ctxt_switches", "0")),
            "procesos_contenedor": sum(1 for name in os.listdir("/proc") if name.isdigit()),
        }
    except (OSError, ValueError, IndexError):
        return {"pid": os.getpid(), "estado_proceso": "no disponible fuera de Linux"}

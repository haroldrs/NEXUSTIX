#!/usr/bin/env python3
"""Sano si Patroni responde: como principal, réplica o mientras se reincorpora."""
import os
import sys
import urllib.request

port = os.environ.get("NEXUSTIX_PATRONI_PORT", "8008")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/liveness", timeout=4) as answer:
        sys.exit(0 if answer.status == 200 else 1)
except Exception:
    sys.exit(1)

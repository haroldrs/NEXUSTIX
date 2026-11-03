"""Docker HEALTHCHECK probe: succeed only if /health answers 200 with status ok.

Usage inside a service image:  python -m shared.healthcheck http://127.0.0.1:8003/health

The service /health endpoints run ``SELECT 1`` against their database, so a
container is reported healthy only when the API process answers HTTP and can
reach PostgreSQL, not merely when the process exists.
"""

import json
import sys
import urllib.request


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("uso: python -m shared.healthcheck URL", file=sys.stderr)
        return 2
    try:
        with urllib.request.urlopen(argv[1], timeout=4) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
            healthy = response.status == 200 and body.get("status") == "ok"
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        print(f"unhealthy: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not healthy:
        print("unhealthy: status distinto de ok", file=sys.stderr)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

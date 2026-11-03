#!/usr/bin/env python3
"""Backup diario por clúster; política de retención informativa, nunca elimina."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from physical_backup import backup


def main():
    root = Path(os.environ["NEXUSTIX_BACKUP_ROOT"]).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination = root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup(destination, os.getenv("NEXUSTIX_PG_BIN"))
    verified = []
    for directory in sorted(root.iterdir()):
        proof = directory / "verification.json"
        if proof.is_file() and json.loads(proof.read_text()).get("verified") is True:
            verified.append(directory.name)
    (root / "retention-review.json").write_text(json.dumps({
        "keep_at_least": 7, "verified_backups": verified,
        "review_candidates": verified[:-7], "deleted": [],
        "warning": "Antes de eliminar: comprobar PITR y conservar WAL desde el backup más antiguo retenido."
    }, indent=2))


if __name__ == "__main__":
    main()

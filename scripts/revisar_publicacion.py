#!/usr/bin/env python3
"""Escaneo local de candidatos Git. Solo rutas, líneas y tipos; nunca valores.

No añade archivos al índice ni contacta servicios. Código 1: coincidencias pendientes;
2: archivo no revisable (binario/enlace/ausente). No certifica ausencia de secretos.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "clave privada": re.compile(r"-{5}BEGIN (?:[A-Z]+ )?PRIVATE KEY-{5}"),
    "token proveedor": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|sk_live_[A-Za-z0-9]{16,})\b"),
    "JWT literal": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    "URL con credencial": re.compile(r"(?:postgres(?:ql)?(?:\+\w+)?|mysql|redis|https?)://[^\s/:]+:[^\s/@]+@"),
    "credencial literal potencial": re.compile(r'''(?i)["']?(?:[\w-]*(?:password|passwd|secret|token|api_key)[\w-]*)["']?\s*[:=]\s*["'][^"'\n]{6,}["']'''),
    "contraseña sin comillas": re.compile(r"(?i)\b(?:POSTGRES_PASSWORD|PGPASSWORD|password|passwd)\s*[:=]\s*[A-Za-z0-9][A-Za-z0-9_.-]*(?=\s|$)"),
}
REVIEWS = "config/publicacion-revisiones.json"
NEVER_APPROVE = {"clave privada", "token proveedor", "JWT literal"}


def fingerprint(path, kind, line):
    return hashlib.sha256((path + "\0" + kind + "\0" + line).encode()).hexdigest()


def read_source(relative, index=False):
    if index:
        return subprocess.check_output(["git", "show", ":" + relative], cwd=ROOT).decode("utf-8")
    path = ROOT / relative
    if path.is_symlink():
        raise OSError("Enlace no inspeccionable")
    return path.read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="store_true", help="Inspeccionar exactamente el índice preparado")
    args = parser.parse_args()
    result = subprocess.run(
        ["git", "ls-files", "--cached", *([] if args.index else ["--others", "--exclude-standard"]), "-z"],
        cwd=ROOT, check=True, capture_output=True,
    )
    paths = sorted(set(result.stdout.decode().split("\0")) - {""})
    reviews = {}
    if REVIEWS in paths:
        entries = json.loads(read_source(REVIEWS, args.index))
        reviews = {entry["huella"]: entry for entry in entries}
    findings, unreadable = [], []
    for relative in paths:
        try:
            source = read_source(relative, args.index)
        except (UnicodeError, OSError):
            unreadable.append({"ruta": relative, "tipo": "binario/no legible: revisión manual"})
            continue
        for number, line in enumerate(source.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    key = fingerprint(relative, kind, line)
                    review = reviews.get(key, {}) if kind not in NEVER_APPROVE else {}
                    accepted = review.get("categoria") in {"dato sintético", "falso positivo"} and bool(review.get("motivo"))
                    findings.append({"ruta": relative, "linea": number, "tipo": kind, "valor": "REDACTADO",
                                     "huella": key, "estado": "REVISADO" if accepted else "PENDIENTE",
                                     "categoria": review.get("categoria", "sin clasificar"), "motivo": review.get("motivo", "")})
    pending = sum(f["estado"] == "PENDIENTE" for f in findings)
    print(json.dumps({"archivos_candidatos": len(paths), "hallazgos": findings,
                      "pendientes": pending, "no_inspeccionados": unreadable}, ensure_ascii=False, indent=2))
    return 2 if unreadable else 1 if pending else 0


if __name__ == "__main__":
    raise SystemExit(main())

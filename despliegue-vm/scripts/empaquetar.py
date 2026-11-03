#!/usr/bin/env python3
"""Artefacto de código para las VMs; lista explícita, sin secretos/resultados/venv."""
import argparse
import hashlib
from pathlib import Path
import tarfile

ROOT=Path(__file__).resolve().parents[2]
SOURCES=('usuarios','eventos','compras','shared','frontend','compras_lb','postgres','despliegue-vm',
         '.dockerignore','scripts/generar_secretos.py','scripts/preparar_distribuido.py',
         'scripts/physical_backup.py','scripts/backup_programado.py')
ALLOWED={'.py','.txt','.html','.js','.css','.sql','.ini','.mako','.yml','.md','.sh','.example','.service','.timer','.json'}


def allowed(path):
    if any(part in {'__pycache__','secrets','master','.venv','.git'} for part in path.parts): return False
    if path.name.endswith(('.pem','.env','-password')) or '-database-url' in path.name: return False
    return path.name in {'Dockerfile','.dockerignore'} or path.suffix in ALLOWED


def pack(destination):
    destination=Path(destination)
    if destination.exists(): raise ValueError('Artefacto existente; no se sobrescribe')
    with tarfile.open(destination,'x:gz') as archive:
        for name in SOURCES:
            source=ROOT/name
            for path in sorted(source.rglob('*')) if source.is_dir() else [source]:
                if path.is_file() and not path.is_symlink() and allowed(path.relative_to(ROOT)):
                    archive.add(path,arcname=str(Path('nexustix')/path.relative_to(ROOT)),recursive=False)
    checksum=hashlib.sha256(destination.read_bytes()).hexdigest()
    print(checksum+'  '+destination.name)
    return checksum


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path)
    pack(p.parse_args().destination)

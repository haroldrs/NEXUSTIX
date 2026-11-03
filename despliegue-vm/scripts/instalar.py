#!/usr/bin/env python3
"""Instalación en una VM Fedora ya configurada. Sin cambios de red/firewall explícitos."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from vm import read_config, PROTECTED


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',required=True,type=Path)
    p.add_argument('--apply',action='store_true')
    args=p.parse_args(); cfg=read_config(args.env_file)
    commands=[
        ['dnf','install','-y','dnf-plugins-core','python3','python3-pyyaml','openssl','chrony','policycoreutils-python-utils'],
        ['dnf','config-manager','addrepo','--from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo'],
        ['dnf','install','-y','docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin'],
        ['systemctl','enable','--now','docker','chronyd'],
    ]
    if not args.apply:
        print(json.dumps({'status':'PLAN_NO_EJECUTADO','role':cfg['VM_ROLE'],'commands':commands},indent=2)); return
    if os.geteuid()!=0: raise SystemExit('Ejecutar como root dentro de la VM destinada al rol.')
    virt=subprocess.run(['systemd-detect-virt','--vm'],capture_output=True,text=True,check=True).stdout.strip()
    if virt not in {'oracle','kvm','qemu','vmware','microsoft','xen','parallels'}: raise SystemExit('Se requiere una VM reconocida.')
    interfaces=json.loads(subprocess.check_output(['ip','-j','-4','address','show'],text=True))
    if cfg['VM_BIND_IP'] not in {a['local'] for i in interfaces for a in i.get('addr_info',[])}:
        raise SystemExit('La IP del rol no está asignada en esta VM. No se cambiará la red.')
    if 'ID=fedora' not in Path('/etc/os-release').read_text().replace('"',''):
        raise SystemExit('Instalador destinado a Fedora con DNF5.')
    if os.getenv('DOCKER_HOST') or os.getenv('DOCKER_CONTEXT'): raise SystemExit('Docker remoto no permitido.')
    existing=subprocess.run(['rpm','-q','docker-ce'],capture_output=True).returncode==0
    if existing:
        # Instalación repetida no actualiza/reinicia un Docker existente.
        print('Docker CE ya instalado. No se actualiza ni reinicia; verificar compose version.'); return
    conflicts=('docker','docker-client','docker-common','docker-engine','moby-engine','podman-docker')
    if any(subprocess.run(['rpm','-q',name],capture_output=True).returncode==0 for name in conflicts):
        raise SystemExit('Motor/paquete incompatible existente. Se conserva; resolver manualmente antes de instalar.')
    for command in commands:
        if command[1:3]==['config-manager','addrepo'] and Path('/etc/yum.repos.d/docker-ce.repo').exists(): continue
        subprocess.run(command,check=True)
    print('Dependencias instaladas. No se añadieron reglas de acceso ni configuraciones de interfaces.')


if __name__=='__main__': main()

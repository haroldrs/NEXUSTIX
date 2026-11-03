#!/usr/bin/env python3
"""Prepara/aplica SOLO la interfaz privada del invitado; nunca la red del hipervisor."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from vm import read_config, PROTECTED


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',required=True,type=Path)
    p.add_argument('--interface',required=True)
    p.add_argument('--expected-mac',required=True)
    p.add_argument('--prefix',type=int,default=24)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args(); cfg=read_config(a.env_file)
    import re,ipaddress
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',a.interface) or not 16<=a.prefix<=30:
        raise SystemExit('Interfaz/prefijo inválido')
    network=ipaddress.ip_network(f"{cfg['VM_BIND_IP']}/{a.prefix}",strict=False)
    if any(ipaddress.ip_address(cfg[k]) not in network for k in ('VM_APP_HOST','VM_PRIMARY_HOST','VM_STANDBY_HOST')):
        raise SystemExit('Los tres hosts deben pertenecer a esta red privada')
    profile='nexustix-vm-private'
    command=['nmcli','connection','add','type','ethernet','con-name',profile,'ifname',a.interface,
        'ipv4.method','manual','ipv4.addresses',f"{cfg['VM_BIND_IP']}/{a.prefix}",
        'ipv4.never-default','yes','ipv4.ignore-auto-dns','yes','ipv6.method','disabled','connection.autoconnect','yes']
    if not a.apply:
        print(json.dumps({'status':'PLAN_NO_EJECUTADO','commands':[command,['nmcli','connection','up',profile]]},indent=2)); return
    virt=subprocess.run(['systemd-detect-virt','--vm'],capture_output=True,text=True,check=True).stdout.strip()
    if os.geteuid()!=0 or virt not in {'oracle','kvm','qemu','vmware','microsoft','xen','parallels'}:
        raise SystemExit('Solo root dentro de una VM reconocida')
    if os.getenv('DOCKER_HOST') or os.getenv('DOCKER_CONTEXT'): raise SystemExit('Docker remoto no permitido')
    if Path('/var/run/docker.sock').exists():
        projects=set(subprocess.check_output(['docker','--host','unix:///var/run/docker.sock','ps','-a','--format','{{.Label \"com.docker.compose.project\"}}'],text=True).splitlines())
        if projects & PROTECTED: raise SystemExit('Hay entornos protegidos: red sin modificar')
    actual=Path(f'/sys/class/net/{a.interface}/address').read_text().strip().lower()
    if actual!=a.expected_mac.lower(): raise SystemExit('MAC no coincide con el adaptador privado del hipervisor')
    route=json.loads(subprocess.check_output(['ip','-j','route','show','default'],text=True))
    if any(r.get('dev')==a.interface for r in route): raise SystemExit('No se modifica la interfaz que lleva la ruta por defecto')
    exists=subprocess.run(['nmcli','-g','connection.id','connection','show',profile],capture_output=True).returncode==0
    if exists:
        addresses=subprocess.check_output(['nmcli','-g','ipv4.addresses','connection','show',profile],text=True).strip()
        device=subprocess.check_output(['nmcli','-g','connection.interface-name','connection','show',profile],text=True).strip()
        if addresses!=f"{cfg['VM_BIND_IP']}/{a.prefix}" or device!=a.interface:
            raise SystemExit('Perfil existente diferente: se conserva sin modificar')
    else: subprocess.run(command,check=True)
    subprocess.run(['nmcli','connection','up',profile],check=True)


if __name__=='__main__': main()

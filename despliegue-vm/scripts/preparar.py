#!/usr/bin/env python3
"""Paquete nuevo por VM; reutiliza el generador de secretos, no ejecuta red/Docker."""
import argparse
import ipaddress
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from preparar_distribuido import generate


def private_ip(value):
    ip = ipaddress.ip_address(value)
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local:
        raise ValueError('Se requieren direcciones IPv4 privadas de las VMs')
    return str(ip)


def prepare(destination, app, primary, standby):
    import yaml
    hosts = dict(zip(('app', 'primary', 'standby'), map(private_ip, (app, primary, standby))))
    if len(set(hosts.values())) != 3:
        raise ValueError('Las tres VMs deben tener direcciones diferentes')
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Destino existente: no se sobrescribe configuración ni secretos')
    destination.mkdir(parents=True, mode=0o700)
    master = destination / 'master'
    generate(master, primary=hosts['primary'], standby=hosts['standby'], public_host=hosts['app'], vm=True)
    # Solo estos nuevos secretos VM: límite de conexión por host para failover.
    for path in master.glob('*-database-url'):
        path.write_text(path.read_text().strip() + '&connect_timeout=3\n')
    for role in ('app', 'primary', 'standby', 'rejoin'):
        directory = destination / role
        directory.mkdir(mode=0o700)
        secrets = directory / 'secrets'; secrets.mkdir(mode=0o700)
        doc = yaml.safe_load((ROOT / f'despliegue-vm/compose.{role}.yml').read_text())
        for item in doc['secrets'].values():
            name = item['file'].rsplit('/', 1)[1]
            target = secrets / name
            shutil.copyfile(master / name, target)
            target.chmod(0o644 if name == 'frontend-config.js' else 0o600)
        bind = hosts['primary' if role == 'rejoin' else role]
        env = {f'VM_{key.upper()}_HOST': value for key, value in hosts.items()}
        env.update(VM_BIND_IP=bind, VM_ROLE=role, VM_REJOIN_GENERATION='r1',
                   NEXUSTIX_DISTRIBUTED_SECRETS_DIR='/etc/nexustix-vm/secrets')
        path = directory / 'config.env'
        path.write_text(''.join(f'{key}={value}\n' for key,value in env.items()))
        path.chmod(0o600)
    (destination / 'inventory.json').write_text(json.dumps({'hosts':hosts,'status':'PREPARADO_NO_DESPLEGADO'},indent=2)+'\n')
    print('Paquetes por rol preparados; master debe permanecer fuera de las VMs.')
    return destination


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--app',default='192.168.56.11')
    parser.add_argument('--primary',default='192.168.56.12')
    parser.add_argument('--standby',default='192.168.56.13')
    args=parser.parse_args()
    prepare(args.destination,args.app,args.primary,args.standby)

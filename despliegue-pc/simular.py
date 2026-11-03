#!/usr/bin/env python3
"""Simulación sin red ni bases: HAProxy y dos respuestas HTTP sintéticas efímeras."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

from lan import PORTS, haproxy_config


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    if not args.apply:
        print('PLAN_NO_EJECUTADO: contenedor nuevo sin red, sin bases y sin volúmenes persistentes')
        return 0
    directory=Path(tempfile.mkdtemp(prefix='nexustix-lan-simulation-'))
    # Direcciones exclusivamente sintéticas, sustituidas por loopback ANTES de Docker.
    cfg={f'LAN_PC{i}_IP':f'192.168.80.{20+i}' for i in (1,2,3)}
    cfg.update({'LAN_'+key+'_PORT':str(value) for key,value in PORTS.items()})
    content=haproxy_config(cfg)
    content=re.sub(r'(server pc([123]) )192\.168\.80\.\d+:(\d+)',
                   lambda m:m[1]+'127.0.0.1:'+str(int(m[2])*10000+int(m[3])),content)
    (directory/'haproxy.cfg').write_text(content)
    (directory/'run.sh').write_text((Path(__file__).resolve().parents[1]/'tests/lan/simular_balanceador.sh').read_text())
    command=['docker','--host','unix:///var/run/docker.sock','run','--rm','--network','none',
             '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
             '--tmpfs','/tmp:rw,nosuid,nodev,size=16m','-e','NEXUSTIX_SIMULATION=1',
             '-v',str(directory)+':/simulation:ro,Z','--entrypoint','sh',
             'haproxy:3.0-alpine','/simulation/run.sh']
    with (directory/'resultado.log').open('w') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=120)
    print('Código:',result.returncode,'Evidencia:',directory)
    return result.returncode


if __name__=='__main__':
    raise SystemExit(main())

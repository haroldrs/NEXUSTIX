#!/usr/bin/env python3
"""Verificación del host escribible desde la VM app y observación de fallos HTTP."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from urllib.request import urlopen
from urllib.error import URLError
from vm import VM,BASES


def database_report(vm, expected):
    if vm.role!='app': raise ValueError('Verificar conexiones desde la VM app')
    vm.guard(); vm.validate()
    report={}
    for i,base in enumerate(BASES):
        program='hosts='+repr({'primary':vm.cfg['VM_PRIMARY_HOST'],'standby':vm.cfg['VM_STANDBY_HOST']})+'\nport='+str(5432+i)+'\n'+'''import json
import psycopg2
from sqlalchemy.engine import make_url
from shared.db_engine import read_database_url
u=make_url(read_database_url())
result={}
for name,host in hosts.items():
 try:
  with psycopg2.connect(host=host,port=port,user=u.username,password=u.password,dbname=u.database,connect_timeout=3) as c:
   with c.cursor() as cur:
    cur.execute("SELECT pg_is_in_recovery()")
    result[name]={'reachable':True,'recovery':cur.fetchone()[0]}
 except psycopg2.Error:
  result[name]={'reachable':False}
print(json.dumps(result))
'''
        service='compras1' if base=='compras' else base
        report[base]=json.loads(vm.run([*vm.compose,'exec','-T',service,'python','-'],program))
    good=all(r[expected].get('recovery') is False and all(not v.get('reachable') or v.get('recovery') is True for k,v in r.items() if k!=expected) for r in report.values())
    return {'expected_primary':expected,'status':'APROBADA' if good else 'FALLIDA','databases':report,
            'limit':'Un nodo no alcanzable no prueba fencing; se requiere evidencia independiente del hipervisor.'}


def observe(vm, seconds):
    # Solo GET públicos; puede ejecutarse desde el equipo de administración.
    rows=[]
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        checks={}
        for service,port in (('usuarios',8001),('eventos',8002),('compras',8005)):
            try:
                with urlopen(f"http://{vm.cfg['VM_APP_HOST']}:{port}/health",timeout=2) as response:
                    checks[service]=response.status
            except (URLError,TimeoutError): checks[service]=0
        rows.append({'utc':datetime.now(timezone.utc).isoformat(),'http':checks})
        time.sleep(1)
    return {'status':'OBSERVADO_NO_CLASIFICADO','samples':rows}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',required=True,type=Path)
    p.add_argument('--expected-primary',choices=('primary','standby'),default='primary')
    p.add_argument('--observe-seconds',type=int,default=0)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args()
    if a.output.exists(): raise SystemExit('La evidencia existente no se sobrescribe')
    if not 0<=a.observe_seconds<=600: raise SystemExit('Duración fuera de rango')
    vm=VM(a.env_file)
    result=observe(vm,a.observe_seconds) if a.observe_seconds else database_report(vm,a.expected_primary)
    with a.output.open('x') as f: json.dump(result,f,indent=2)
    a.output.chmod(0o600)
    print(result['status'])
    raise SystemExit(1 if result['status']=='FALLIDA' else 0)

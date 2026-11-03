#!/usr/bin/env python3
"""Compra antes/después de conmutación con mismos IDs; solo VMs y --apply."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
from urllib.request import Request,urlopen
from uuid import uuid4
from vm import VM
from verificar import database_report


def save(path, data, exclusive=False):
    with path.open('x' if exclusive else 'w') as f: json.dump(data,f,indent=2)
    path.chmod(0o600)


def exercise(vm,stage,state_path,email,expected):
    vm.guard(); vm.validate()
    checks=database_report(vm,expected)
    if checks['status']!='APROBADA': raise ValueError('Topología PostgreSQL no coincide con el primario esperado')
    token=None
    def api(port,method,path,data=None,key=None):
        headers={'Content-Type':'application/json'}
        if token: headers['Authorization']='Bearer '+token
        if key: headers['Idempotency-Key']=key
        request=Request(f"http://{vm.cfg['VM_APP_HOST']}:{port}{path}",data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method)
        with urlopen(request,timeout=40) as response: return json.load(response)
    secret=Path(vm.cfg['NEXUSTIX_DISTRIBUTED_SECRETS_DIR'])/'admin-password'
    token=api(8001,'POST','/auth/login',{'email':email,'password':secret.read_text().strip()})['access_token']
    if stage=='before':
        if state_path.exists(): raise ValueError('Estado existente; conservarlo para replay')
        # Persistir intención antes de empezar a escribir: una repetición usa la misma clave.
        state={'status':'INICIADA','key':uuid4().hex,'after_key':uuid4().hex,'app':vm.cfg['VM_APP_HOST']}
        save(state_path,state,True)
        event=api(8002,'POST','/eventos',{'nombre':'Aceptación VM '+state['key'][:8],'fecha':'2027-06-01T20:00:00-05:00','capacidad':3,'precio_centimos':100})
        state['items']=[{'evento_id':event['id'],'cantidad':1}]; save(state_path,state)
        order=api(8005,'POST','/ordenes',{'items':state['items']},state['key'])
        state['order_id']=order['id']; save(state_path,state)
        paid=api(8005,'POST',f"/ordenes/{order['id']}/pagar",{'resultado':'aprobado'})
        assert paid['estado']=='completada'
        state['status']='BASELINE_APROBADA'; save(state_path,state)
    else:
        state=json.loads(state_path.read_text())
        assert state['status'] in ('BASELINE_APROBADA','CONMUTACION_APROBADA')
        assert state['app']==vm.cfg['VM_APP_HOST'] and state.get('order_id')
        replay=api(8005,'POST','/ordenes',{'items':state['items']},state['key'])
        assert replay['id']==state['order_id'] and replay['estado']=='completada'
        assert api(8005,'POST',f"/ordenes/{replay['id']}/pagar",{'resultado':'aprobado'})['estado']=='completada'
        second=api(8005,'POST','/ordenes',{'items':state['items']},state['after_key'])
        assert api(8005,'POST',f"/ordenes/{second['id']}/pagar",{'resultado':'aprobado'})['estado']=='completada'
        tickets=api(8005,'GET','/entradas/mias')
        for oid in (replay['id'],second['id']):
            assert sum(t['orden_id']==oid for t in tickets)==1
        eid=state['items'][0]['evento_id']
        assert api(8002,'GET',f'/eventos/{eid}')['stock']==1
        assert api(8002,'GET','/inventario/estado')['consistente']
        state['after_order_id']=second['id']; state['status']='CONMUTACION_APROBADA'; save(state_path,state)
    return {'status':state['status'],'utc':datetime.now(timezone.utc).isoformat(),'databases':checks,'order_id':state['order_id']}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',required=True,type=Path)
    p.add_argument('--stage',required=True,choices=('before','after'))
    p.add_argument('--state',required=True,type=Path)
    p.add_argument('--admin-email',default='vm-admin@example.com')
    p.add_argument('--expected-primary',choices=('primary','standby'),required=True)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args(); vm=VM(a.env_file)
    if not a.apply: print('PLAN_NO_EJECUTADO: crea compras sintéticas exclusivamente en las VMs.'); raise SystemExit(0)
    if a.output.exists(): raise SystemExit('Evidencia existente; usar otra ruta')
    try:
        result=exercise(vm,a.stage,a.state,a.admin_email,a.expected_primary)
    except Exception as error:
        result={'status':'FALLIDA','error_type':type(error).__name__,'utc':datetime.now(timezone.utc).isoformat()}
    save(a.output,result,True)
    print(result['status'])
    raise SystemExit(1 if result['status']=='FALLIDA' else 0)

#!/usr/bin/env python3
"""LAN aislada: preparar archivos; validar; planes por defecto, operación con --apply."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from generar_secretos import generate_rsa_pair, write_secret
import secrets

PORTS = {'FRONTEND':8080, 'USUARIOS':8001, 'EVENTOS':8002, 'COMPRAS':8003,
         'BALANCER':8005, 'PG_USUARIOS':5432, 'PG_EVENTOS':5433, 'PG_COMPRAS':5434}
ROLES = {'pc1': {'compras','gateway','frontend'},
         'pc2': {'usuarios','eventos','compras','usuarios_db','eventos_db','compras_db'},
         'pc3': {'usuarios','eventos'}}
PROTECTED = {'nexustix', 'project', 'nexustix-auth-staging', 'nexustix-distributed-lab'}
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']


def run(args, env=None, check=True):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=900)
    if check and result.returncode:
        raise RuntimeError('Comando falló; salida omitida para no exponer secretos')
    return result


def validate_config(cfg):
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,24}', cfg.get('LAN_DEPLOYMENT_ID','')):
        raise ValueError('LAN_DEPLOYMENT_ID: 3–25 caracteres a-z, dígitos y guion')
    if cfg.get('LAN_NODE') not in ROLES:
        raise ValueError('Nodo desconocido')
    if not re.fullmatch(r'[a-f0-9]{32}',cfg.get('LAN_INSTALLATION_UUID','')):
        raise ValueError('Identidad de instalación inválida')
    addresses=[]
    for node in ROLES:
        address=ipaddress.IPv4Address(cfg[f'LAN_{node.upper()}_IP'])
        if not any(address in ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
            raise ValueError('Se requieren IPv4 LAN privadas RFC1918, sin loopback')
        addresses.append(str(address))
    if len(set(addresses))!=3:
        raise ValueError('Las tres PC requieren IPs distintas')
    ports=[int(cfg['LAN_'+key+'_PORT']) for key in PORTS]
    if len(set(ports))!=len(ports) or not all(1024 <= p <= 65535 for p in ports):
        raise ValueError('Puertos distintos entre 1024 y 65535')
    return cfg


def haproxy_config(cfg):
    lines=['global', '    log stdout format raw local0', '    maxconn 512',
           'defaults', '    mode http', '    log global', '    timeout connect 3s',
           '    timeout client 40s', '    timeout server 40s', '    retries 0', '']
    targets={'usuarios':('pc2','pc3'),'eventos':('pc2','pc3'),'compras':('pc1','pc2')}
    for svc,nodes in targets.items():
        listener='BALANCER' if svc=='compras' else svc.upper()
        lines += [f'frontend {svc}_in',f"    bind :{cfg['LAN_'+listener+'_PORT']}",
                  "    http-request return status 200 content-type application/json string '{\"status\":\"ok\"}' if { path -i /lb-health }",
                  f'    default_backend {svc}_pool',f'backend {svc}_pool','    balance roundrobin',
                  '    option httpchk GET /health','    http-check expect status 200',
                  '    http-response set-header X-Nexustix-Backend %[srv_name]']
        for node in nodes:
            lines.append(f"    server {node} {cfg['LAN_'+node.upper()+'_IP']}:{cfg['LAN_'+svc.upper()+'_PORT']} check inter 2s fall 2 rise 2")
        lines.append('')
    return '\n'.join(lines)+'\n'


def prepare(destination, deployment, addresses, ports):
    destination=Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_relative_to(ROOT):
        raise ValueError('Destino nuevo y fuera del repositorio requerido')
    cfg={'LAN_DEPLOYMENT_ID':deployment,'LAN_INSTALLATION_UUID':uuid4().hex,'LAN_NODE':'pc1'}
    cfg.update({f'LAN_PC{i+1}_IP':ip for i,ip in enumerate(addresses)})
    cfg.update({'LAN_'+key+'_PORT':str(ports[key]) for key in PORTS})
    validate_config(cfg)
    destination.mkdir(parents=True,mode=0o700)
    master=destination/'master';master.mkdir(mode=0o700)
    generate_rsa_pair(master/'jwt-private.pem',master/'jwt-public.pem')
    for svc in ('usuarios','eventos','compras'):
        password=secrets.token_urlsafe(32)
        write_secret(master/f'{svc}-db-password',password)
        write_secret(master/f'{svc}-admin-password',secrets.token_urlsafe(32))
        url=f"postgresql://nexustix_app:{password}@{addresses[1]}:{ports['PG_'+svc.upper()]}/{svc}_db"
        write_secret(master/f'{svc}-database-url',url)
    for name in ('eventos-service-token','admin-password'):
        write_secret(master/name,secrets.token_urlsafe(40))
    for node in ROLES:
        base=destination/node;base.mkdir(mode=0o700);(base/'secrets').mkdir(mode=0o700)
        node_cfg={**cfg,'LAN_NODE':node}
        write_secret(base/'config.env','\n'.join(f'{k}={v}' for k,v in node_cfg.items())+'\n')
        names={'jwt-public.pem'}
        if 'compras' in ROLES[node]:names|={'compras-database-url','eventos-service-token'}
        if 'usuarios' in ROLES[node]:names|={'usuarios-database-url','eventos-database-url','jwt-private.pem','eventos-service-token'}
        if node=='pc2':names|={'admin-password'}|{f'{s}-{n}' for s in ('usuarios','eventos','compras') for n in ('db-password','admin-password')}
        for name in names:shutil.copyfile(master/name,base/'secrets'/name);(base/'secrets'/name).chmod(0o600)
        if node=='pc1':
            write_secret(base/'haproxy.cfg',haproxy_config(cfg),mode=0o644)
            urls={'USUARIOS_URL':f"http://{addresses[0]}:{ports['USUARIOS']}",
                  'EVENTOS_URL':f"http://{addresses[0]}:{ports['EVENTOS']}",
                  'COMPRAS_LB_URL':f"http://{addresses[0]}:{ports['BALANCER']}",
                  'COMPRAS_R1_URL':f"http://{addresses[0]}:{ports['COMPRAS']}",
                  'COMPRAS_R2_URL':f"http://{addresses[1]}:{ports['COMPRAS']}",'CHECKOUT_ORDERS':True}
            write_secret(base/'secrets/frontend-config.js','const CONFIG = Object.freeze('+json.dumps(urls)+');\n',mode=0o644)
    return destination


class Node:
    def __init__(self,path):
        self.path=Path(path).expanduser().resolve()
        if self.path.is_relative_to(ROOT):raise ValueError('Configuración privada fuera del repositorio requerida')
        cfg={}
        for line in self.path.read_text().splitlines():
            if not line or line.startswith('#'):continue
            key,sep,value=line.partition('=')
            if not sep or key in cfg or not re.fullmatch(r'LAN_[A-Z0-9_]+',key) or not re.fullmatch(r'[A-Za-z0-9.-]+',value):
                raise ValueError('Configuración inválida, duplicada o interpolada')
            cfg[key]=value
        self.cfg=validate_config(cfg);self.role=cfg['LAN_NODE']
        self.project=f"nexustix-lan-{cfg['LAN_DEPLOYMENT_ID']}-{self.role}"
        self.base=self.path.parent

    def env(self):
        # Compose no hereda variables arbitrarias del shell, contexto ni .env del repo.
        env={k:v for k,v in os.environ.items() if not k.startswith(('LAN_','COMPOSE_','DOCKER_'))}
        env.update(self.cfg);env.update(LAN_BIND_IP=self.cfg['LAN_'+self.role.upper()+'_IP'],
            LAN_SECRETS_DIR=str(self.base/'secrets'),LAN_CONFIG_DIR=str(self.base))
        return env

    def compose(self,*args):
        return run(DOCKER+['compose','--env-file',str(self.path),'-p',self.project,'-f',
                   str(ROOT/f'despliegue-pc/compose.{self.role}.yml'),*args],self.env())

    def validate(self):
        if self.path.stat().st_mode & 0o077 or self.base.stat().st_mode & 0o077:
            raise ValueError('Directorio privado 0700 y config.env 0600 requeridos')
        if self.role=='pc1' and (self.base/'haproxy.cfg').read_text()!=haproxy_config(self.cfg):
            raise ValueError('HAProxy no coincide con config.env; renderice de nuevo')
        doc=json.loads(self.compose('config','--format','json').stdout)
        if doc['name']!=self.project or set(doc['services'])!=ROLES[self.role]:
            raise ValueError('Topología inesperada')
        for spec in doc.get('secrets',{}).values():
            path=Path(spec['file']).resolve()
            if not path.is_relative_to(self.base/'secrets') or not path.is_file():raise ValueError('Secreto ausente o ruta externa al nodo')
            if path.name!='frontend-config.js' and path.stat().st_mode & 0o077:raise ValueError('Secreto requiere 0600')
        for spec in doc['services'].values():
            if spec.get('privileged') or spec.get('network_mode')=='host':raise ValueError('Privilegios/red no permitidos')
            if spec.get('labels',{}).get('org.nexustix.installation')!=self.cfg['LAN_INSTALLATION_UUID']:raise ValueError('Identidad de servicio incorrecta')
            for port in spec.get('ports',[]):
                if port.get('host_ip')!=self.env()['LAN_BIND_IP']:raise ValueError('Bind distinto de la IP del nodo')
        for kind in ('networks','volumes'):
            for spec in doc.get(kind,{}).values():
                if not spec['name'].startswith(self.project+'_') or spec.get('external'):raise ValueError('Recurso compartido no autorizado')
        return doc

    def guard(self):
        if os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
            raise ValueError('Quite DOCKER_HOST/DOCKER_CONTEXT; solo daemon Unix local soportado')
        addresses=json.loads(run(['ip','-j','address','show']).stdout)
        expected=self.env()['LAN_BIND_IP']
        if not any(a.get('local')==expected for iface in addresses for a in iface.get('addr_info',[])):
            raise ValueError('IP local no coincide con el rol')
        rows=run(DOCKER+['ps','-a','--format','{{json .}}']).stdout.splitlines()
        for row in rows:
            data=json.loads(row);labels=dict(item.split('=',1) for item in data.get('Labels','').split(',') if '=' in item)
            project=labels.get('com.docker.compose.project','')
            if project in PROTECTED or project.startswith('nexustix-vm-'):
                raise ValueError('Host protegido: no operar LAN junto a demo/staging/laboratorio/VM')
            if project==self.project and (labels.get('org.nexustix.installation')!=self.cfg['LAN_INSTALLATION_UUID'] or labels.get('com.docker.compose.service') not in ROLES[self.role]):
                raise ValueError('Contenedor de otra instalación')
        doc=self.validate()
        for kind,command in (('volumes','volume'),('networks','network')):
            for spec in doc.get(kind,{}).values():
                res=run(DOCKER+[command,'inspect',spec['name']],check=False)
                if res.returncode==0:
                    labels=json.loads(res.stdout)[0].get('Labels') or {}
                    if labels.get('org.nexustix.installation')!=self.cfg['LAN_INSTALLATION_UUID']:
                        raise ValueError('Recurso preexistente pertenece a otra instalación')
        return doc

    def stop_purchase(self):
        if self.role!='pc1':raise ValueError('El ensayo solo detiene Compras de PC 1')
        self.guard();self.compose('stop','compras')

    def start_purchase(self):
        self.guard();self.compose('start','compras')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirección HTTP rechazada')


def probe(url,token=None):
    request=Request(url,headers={'Authorization':'Bearer '+token} if token else {})
    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=8) as response:
        return json.load(response)


def failover(node,token=None,read=probe,sleep=time.sleep):
    if node.role!='pc1':raise ValueError('Ejecutar en PC 1')
    node.guard()
    cfg=node.cfg
    direct=[f"http://{cfg[f'LAN_PC{i}_IP']}:{cfg['LAN_COMPRAS_PORT']}/health" for i in (1,2)]
    for i,url in enumerate(direct,1):
        value=read(url)
        if value.get('status')!='ok' or str(value.get('instancia'))!=str(i):raise ValueError('Preflight: ambas réplicas deben estar sanas y corresponder al rol')
    gateway=f"http://{cfg['LAN_PC1_IP']}:{cfg['LAN_BALANCER_PORT']}"
    if token:read(gateway+'/ordenes/mias',token)
    restored=False
    try:
        node.stop_purchase()
        deadline=time.monotonic()+35
        while True:
            try:
                value=read(gateway+'/health')
                if value.get('status')=='ok' and str(value.get('instancia'))=='2':break
            except (OSError,ValueError):pass
            if time.monotonic()>deadline:raise RuntimeError('PC 2 no atendió dentro del límite')
            sleep(1)
        for _ in range(5):
            value=read(gateway+'/health')
            if value.get('status')!='ok' or str(value.get('instancia'))!='2':raise RuntimeError('Continuidad no verificada')
        if token:read(gateway+'/ordenes/mias',token)
    finally:
        node.start_purchase()
        deadline=time.monotonic()+90
        while True:
            try:
                value=read(direct[0])
                if value.get('status')=='ok' and str(value.get('instancia'))=='1':restored=True;break
            except (OSError,ValueError):pass
            if time.monotonic()>deadline:break
            sleep(1)
        if not restored:raise RuntimeError('Compras PC 1 no recuperó salud; diagnóstico requerido')
    return {'estado':'APROBADA','solicitudes_pc2':5,'pc1_recuperada':restored,
            'lectura_ordenes_autenticada':bool(token),'alcance':'HTTP health con SELECT 1; no crea compras'}


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    pre=sub.add_parser('prepare');pre.add_argument('--destination',type=Path,required=True);pre.add_argument('--deployment',required=True)
    for i in (1,2,3):pre.add_argument(f'--pc{i}',required=True)
    for key,value in PORTS.items():pre.add_argument('--'+key.lower().replace('_','-')+'-port',type=int,default=value)
    for action in ('validate','render','up','stop','start','status','verify','admin','failover','gateway-check'):
        a=sub.add_parser(action);a.add_argument('--env-file',type=Path,required=True);a.add_argument('--apply',action='store_true')
        if action=='admin':a.add_argument('--email',required=True)
        if action=='failover':a.add_argument('--output',type=Path,required=True);a.add_argument('--token-file',type=Path)
    a=p.parse_args()
    if a.command=='prepare':
        dest=prepare(a.destination,a.deployment,[a.pc1,a.pc2,a.pc3],{key:getattr(a,key.lower()+'_port') for key in PORTS})
        print('Archivos nuevos preparados; distribuir cada carpeta solo a su nodo:',dest);return 0
    node=Node(a.env_file)
    if a.command=='validate':node.validate();print('CONFIGURACION_VALIDA_SIN_DESPLIEGUE');return 0
    if a.command=='render':
        if node.role!='pc1':raise ValueError('Solo PC 1 tiene gateway')
        (node.base/'haproxy.cfg').write_text(haproxy_config(node.cfg));print('Configuración HAProxy actualizada');return 0
    if not a.apply:
        print('PLAN_NO_EJECUTADO',a.command,node.project);return 0
    node.guard()
    if a.command=='up':node.compose('up','-d','--build','--wait','--wait-timeout','180')
    elif a.command in ('stop','start'):node.compose(a.command)
    elif a.command=='status':print(node.compose('ps').stdout)
    elif a.command=='gateway-check':
        if node.role!='pc1':raise ValueError('Solo PC 1')
        node.compose('run','--rm','--no-deps','gateway','haproxy','-c','-f','/usr/local/etc/haproxy/haproxy.cfg')
    elif a.command=='admin':
        if node.role!='pc2':raise ValueError('Bootstrap solo PC 2')
        node.compose('exec','-T','usuarios','python','bootstrap_admin.py','--email',a.email,'--password-file','/run/secrets/admin_password','--if-missing')
    elif a.command=='verify':
        ip=node.env()['LAN_BIND_IP']
        for service in ROLES[node.role]&{'usuarios','eventos','compras'}:
            value=probe(f"http://{ip}:{node.cfg['LAN_'+service.upper()+'_PORT']}/health")
            if value.get('status')!='ok':raise ValueError('API no saludable')
            print(service,'OK')
        if node.role=='pc1':
            for key in ('USUARIOS','EVENTOS','BALANCER','FRONTEND'):
                value=probe(f"http://{ip}:{node.cfg['LAN_'+key+'_PORT']}/health")
                if value.get('status')!='ok':raise ValueError('Entrada no saludable')
                print(key,'OK')
    elif a.command=='failover':
        if a.output.exists() or a.output.resolve().is_relative_to(ROOT):raise ValueError('Evidencia nueva fuera del repo requerida')
        token=a.token_file.read_text().strip() if a.token_file else None
        # Reservar el archivo antes de detener nada; no perder evidencia por una ruta inválida.
        with a.output.open('x') as log:
            a.output.chmod(0o600)
            try:result=failover(node,token)
            except Exception as error:result={'estado':'FALLIDA','tipo':type(error).__name__}
            json.dump(result,log,indent=2)
        print(result['estado']);return 0 if result['estado']=='APROBADA' else 1
    print('OPERACION_COMPLETADA');return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,KeyError,OSError,RuntimeError) as error:
        print('RECHAZADO:',str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__,file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Operación local de una VM. Mutaciones solo con --apply, VM e IP verificadas."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASES = ('usuarios', 'eventos', 'compras')
ROLES = ('app', 'primary', 'standby', 'rejoin')
PROTECTED = {'nexustix', 'nexustix-auth-staging', 'nexustix-distributed-lab'}


def read_config(path):
    cfg = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z_]+',key) or key in cfg:
            raise ValueError('config.env requiere KEY=VALUE único, sin sintaxis shell')
        if any(c in value for c in ('$', '`', '\n', '\r', '"', "'")):
            raise ValueError('config.env no admite interpolación ni comillas')
        cfg[key] = value
    if cfg.get('VM_ROLE') not in ROLES: raise ValueError('VM_ROLE inválido')
    hosts = []
    for key in ('VM_APP_HOST', 'VM_PRIMARY_HOST', 'VM_STANDBY_HOST', 'VM_BIND_IP'):
        addr=ipaddress.ip_address(cfg[key])
        if addr.version != 4 or not addr.is_private or addr.is_loopback or addr.is_unspecified or addr.is_link_local:
            raise ValueError('IP privada IPv4 requerida')
        hosts.append(str(addr))
    if len(set(hosts[:3])) != 3: raise ValueError('Hosts deben ser diferentes')
    expected = cfg['VM_PRIMARY_HOST' if cfg['VM_ROLE']=='rejoin' else f"VM_{cfg['VM_ROLE'].upper()}_HOST"]
    if cfg['VM_BIND_IP'] != expected: raise ValueError('VM_BIND_IP no corresponde al rol')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,20}',cfg.get('VM_REJOIN_GENERATION','r1')):
        raise ValueError('Generación inválida')
    secret_dir=Path(cfg['NEXUSTIX_DISTRIBUTED_SECRETS_DIR'])
    if not secret_dir.is_absolute(): raise ValueError('Ruta de secretos debe ser absoluta')
    return cfg


class VM:
    def __init__(self, config):
        self.path=Path(config).resolve()
        self.cfg=read_config(self.path)
        self.role=self.cfg['VM_ROLE']
        self.project=f'nexustix-vm-{self.role}'
        self.manifest=ROOT/f'despliegue-vm/compose.{self.role}.yml'
        self.env={k:v for k,v in os.environ.items() if not k.startswith(('COMPOSE_', 'VM_', 'NEXUSTIX_DISTRIBUTED_'))}
        self.env.update(self.cfg)
        self.compose=['docker','compose','--project-name',self.project,'--env-file',str(self.path),'-f',str(self.manifest)]

    def run(self, args, input=None):
        p=subprocess.run(args,env=self.env,input=input,text=True,capture_output=True,timeout=600)
        if p.returncode:
            # Los errores de drivers pueden incluir DSN; nunca volcarlos.
            raise RuntimeError(f'{Path(args[0]).name}: código {p.returncode}; revisar diagnóstico local sin publicar secretos')
        return p.stdout.strip()

    def guard(self, allow_disconnected=False):
        virt=self.run(['systemd-detect-virt','--vm'])
        if virt not in {'oracle','kvm','qemu','vmware','microsoft','xen','parallels'}:
            raise ValueError('Operación permitida solo dentro de una VM reconocida')
        addresses=json.loads(self.run(['ip','-j','-4','address','show']))
        if not allow_disconnected and self.cfg['VM_BIND_IP'] not in {a['local'] for i in addresses for a in i.get('addr_info',[])}:
            raise ValueError('Esta máquina no tiene VM_BIND_IP; no ejecutar en Fedora anfitrión')
        if os.getenv('DOCKER_HOST') or os.getenv('DOCKER_CONTEXT'):
            raise ValueError('No se admiten DOCKER_HOST/DOCKER_CONTEXT externos')
        context=json.loads(self.run(['docker','context','inspect']))
        if not context[0]['Endpoints']['docker']['Host'].startswith('unix://'):
            raise ValueError('Docker debe ser local a esta VM')
        projects=set(self.run(['docker','ps','-a','--format','{{.Label "com.docker.compose.project"}}']).splitlines())
        if projects & PROTECTED: raise ValueError('Hay entornos protegidos en este Docker; operación rechazada')

    def validate(self):
        doc=json.loads(self.run([*self.compose,'config','--format','json']))
        if doc['name'] != self.project: raise ValueError('Proyecto inesperado')
        for svc in doc['services'].values():
            if not svc['image'].startswith('nexustix-vm/') or 'container_name' in svc:
                raise ValueError('Imagen/nombre no aislado para VMs')
            for port in svc.get('ports',[]):
                if port['host_ip'] != self.cfg['VM_BIND_IP']: raise ValueError('Puerto no limitado a la IP privada')
        for vol in doc.get('volumes',{}).values():
            if not vol['name'].startswith(self.project+'_'): raise ValueError('Volumen no aislado')
        for secret in doc.get('secrets',{}).values():
            path=Path(secret['file'])
            if not path.is_file() or path.stat().st_size==0: raise ValueError('Falta un secreto requerido')
            if path.name!='frontend-config.js' and path.stat().st_mode & 0o077:
                raise ValueError('Secretos privados deben tener modo 0600')
        return doc

    def services(self):
        return [base+'_db'+('_replica' if self.role in ('standby','rejoin') else '') for base in BASES]

    def sql(self, service, sql):
        database=service.removesuffix('_replica')
        return self.run([*self.compose,'exec','-T',service,'psql','-X','-q','-A','-t','-v','ON_ERROR_STOP=1','-U','postgres','-d',database,'-c',sql])

    def diagnosis(self):
        self.guard(); self.validate()
        report={'role':self.role,'ip':self.cfg['VM_BIND_IP'],'containers':self.run([*self.compose,'ps','--format','json'])}
        if self.role!='app':
            report['databases']={}
            for service in self.services():
                report['databases'][service]=json.loads(self.sql(service,"""SELECT json_build_object(
                    'recovery',pg_is_in_recovery(), 'archive_mode',current_setting('archive_mode'),
                    'receive_lsn',pg_last_wal_receive_lsn(), 'replay_lsn',pg_last_wal_replay_lsn(),
                    'replication',COALESCE((SELECT json_agg(json_build_object('state',state,'sync_state',sync_state,
                        'lag_bytes',CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_wal_lsn_diff(pg_current_wal_lsn(),replay_lsn) END)) FROM pg_stat_replication),'[]'::json),
                    'receiver',COALESCE((SELECT json_agg(json_build_object('status',status,'sender_host',sender_host,'sender_port',sender_port)) FROM pg_stat_wal_receiver),'[]'::json),
                    'slots',COALESCE((SELECT json_agg(json_build_object('name',slot_name,'active',active,'wal_status',wal_status)) FROM pg_replication_slots),'[]'::json),
                    'archiver',(SELECT row_to_json(a) FROM (SELECT archived_count,failed_count,last_archived_time,last_failed_time FROM pg_stat_archiver) a));"""))
        else:
            # Usa el mismo lector de URL/opciones que la aplicación, sin imprimir DSN.
            program="""import json,os
from sqlalchemy import create_engine,text
from shared.db_engine import read_database_url,engine_options
u=read_database_url()
e=create_engine(u,**engine_options(u))
with e.connect() as c:
 print(json.dumps({'writable':not c.execute(text('SELECT pg_is_in_recovery()')).scalar_one()}))
"""
            report['connections']={s:json.loads(self.run([*self.compose,'exec','-T',s,'python','-'],program)) for s in ('usuarios','eventos','compras1','compras2')}
        return report

    def deploy(self):
        self.guard(); doc=self.validate()
        if self.role=='primary' and self.path.with_name('primary.FENCED').exists():
            raise ValueError('Primario retirado: usar rejoin con volúmenes nuevos')
        if self.role=='rejoin':
            if not self.path.with_name('primary.FENCED').exists(): raise ValueError('Primero retirar y aislar el primario antiguo')
            # Una generación se inicia una sola vez. Reanudarla requiere start,
            # no crear un nuevo intento sobre una copia parcial.
            names=self.run(['docker','volume','ls','--format','{{.Name}}']).splitlines()
            if any(v['name'] in names for v in doc['volumes'].values()):
                raise ValueError('Generación existente; inspeccionar o usar otra, nunca vaciarla')
        self.run([*self.compose,'up','--build','--detach','--wait','--wait-timeout','300'])

    def promote(self, fence_path):
        if self.role!='standby': raise ValueError('Promoción inicial solo en VM standby')
        validate_fence(fence_path,self.cfg)
        self.guard(); self.validate()
        if any(self.sql(s,'SELECT pg_is_in_recovery();')!='t' for s in self.services()):
            raise ValueError('Las tres bases deben ser standby antes de promover; revisar cualquier promoción parcial')
        results={}
        for service in self.services():
            if self.sql(service,'SELECT pg_promote(true,60);')!='t': raise RuntimeError('Promoción no completada')
            self.sql(service,"ALTER SYSTEM SET synchronous_standby_names = '';")
            self.sql(service,'SELECT pg_reload_conf();')
            slot=service.removesuffix('_replica')+'_rejoin'
            self.sql(service,f"SELECT pg_create_physical_replication_slot('{slot}') WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='{slot}');")
            if self.sql(service,'SELECT pg_is_in_recovery();')!='f': raise RuntimeError('Nodo no escribible')
            results[service]='promoted'
        return results

    def retire(self):
        if self.role!='primary': raise ValueError('Retirar solo la antigua VM primary')
        # Retirar con NIC desconectada: puede no conservar su IP configurada.
        self.guard(allow_disconnected=True); self.validate()
        ids=self.run([*self.compose,'ps','--all','--quiet']).splitlines()
        if len(ids)!=3: raise ValueError('Se requieren los tres contenedores originales; no se crearán volúmenes para aislar')
        self.run(['docker','update','--restart=no',*ids])
        self.run([*self.compose,'stop'])
        for service in self.services():
            self.run([*self.compose,'run','--rm','--no-deps','--entrypoint','sh',service,'-c',
                      'test -s "$PGDATA/PG_VERSION" && touch "$PGDATA/NEXUSTIX_FENCED"'])
        marker=self.path.with_name('primary.FENCED')
        marker.write_text('Antiguo primario retirado; conservar sus volúmenes.\n')
        marker.chmod(0o600)


def validate_fence(path,cfg):
    if not path: raise ValueError('Falta registro de fencing del operador/hipervisor')
    proof=json.loads(Path(path).read_text())
    if proof.get('primary_host')!=cfg['VM_PRIMARY_HOST'] or proof.get('standby_host')!=cfg['VM_STANDBY_HOST']:
        raise ValueError('Fencing de otros nodos')
    for key in ('primary_powered_off','primary_autostart_disabled','application_stopped','accept_async_data_loss'):
        if proof.get(key) is not True: raise ValueError('Fencing y aceptación de RPO deben ser explícitos')
    for key in ('operator','hypervisor_evidence','primary_vm_uuid'):
        if not isinstance(proof.get(key),str) or not proof[key].strip(): raise ValueError('Falta evidencia/identidad de operador')
    stamp=datetime.fromisoformat(proof['observed_at'])
    if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 3600:
        raise ValueError('Registro de fencing vencido o sin zona horaria; verificar otra vez')
    return proof


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',type=Path,required=True)
    p.add_argument('action',choices=('validate','diagnose','deploy','start','stop','retire','promote'))
    p.add_argument('--apply',action='store_true')
    p.add_argument('--fence-record',type=Path)
    args=p.parse_args(); vm=VM(args.env_file)
    if args.action=='validate':
        doc=vm.validate(); print(json.dumps({'status':'CONFIGURACION_VALIDA_NO_DESPLEGADA','project':vm.project,'services':list(doc['services'])}))
    elif args.action=='diagnose': print(json.dumps(vm.diagnosis(),indent=2))
    elif not args.apply:
        print(json.dumps({'status':'PLAN_NO_EJECUTADO','action':args.action,'role':vm.role,'ip':vm.cfg['VM_BIND_IP'],'project':vm.project}))
    elif args.action=='deploy': vm.deploy(); print('Despliegue terminado; ejecutar diagnose y aceptación.')
    elif args.action=='retire': vm.retire(); print('Primario retirado; volúmenes conservados.')
    elif args.action=='promote': print(json.dumps(vm.promote(args.fence_record)))
    else:
        vm.guard(); vm.validate()
        if args.action=='start' and vm.role=='primary' and vm.path.with_name('primary.FENCED').exists():
            raise ValueError('Primario retirado; no se puede arrancar')
        vm.run([*vm.compose,args.action]); print('Operación completada.')


if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,KeyError,OSError,subprocess.SubprocessError):
        # No incluir errores de Docker/SQL/secretos potencialmente sensibles.
        print('Operación rechazada o incompleta. Revise rol/IP, secretos, estado VM y diagnóstico; no se limpiaron datos.',file=sys.stderr)
        sys.exit(1)

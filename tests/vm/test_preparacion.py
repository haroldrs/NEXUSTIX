"""Preparación/guardas VM sin Docker activo, red, cambios del host ni datos previos."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime,timedelta,timezone

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'despliegue-vm/scripts'))
from preparar import prepare
from vm import VM,read_config,validate_fence


@pytest.fixture
def bundle(tmp_path):
    return prepare(tmp_path/'package','192.168.56.11','192.168.56.12','192.168.56.13')


def config(bundle, role):
    path=bundle/role/'config.env'
    path.write_text(path.read_text().replace('/etc/nexustix-vm/secrets',str(bundle/role/'secrets')))
    return path


def test_secrets_are_separated_and_multi_host_urls_have_timeout(bundle):
    assert (bundle/'app/secrets/jwt-private.pem').is_file()
    for role in ('primary','standby','rejoin'):
        assert not (bundle/role/'secrets/jwt-private.pem').exists()
        assert not (bundle/role/'secrets/usuarios-database-url').exists()
    url=(bundle/'app/secrets/compras-database-url').read_text()
    assert 'host=192.168.56.12:5434&host=192.168.56.13:5434' in url
    assert 'target_session_attrs=read-write' in url and 'connect_timeout=3' in url
    assert (bundle/'app/secrets/admin-password').stat().st_mode & 0o077==0


def test_preparation_refuses_existing_directory(bundle):
    before=(bundle/'master/admin-password').read_bytes()
    with pytest.raises(ValueError,match='existente'): prepare(bundle,'192.168.56.11','192.168.56.12','192.168.56.13')
    assert (bundle/'master/admin-password').read_bytes()==before


@pytest.mark.parametrize('hosts',[('127.0.0.1','192.168.56.12','192.168.56.13'),('192.168.56.11',)*3,('8.8.8.8','192.168.56.12','192.168.56.13')])
def test_preparation_rejects_wrong_topologies(tmp_path,hosts):
    with pytest.raises(ValueError): prepare(tmp_path/'bad',*hosts)
    assert not (tmp_path/'bad').exists()


def test_wrong_binding_and_shell_values_rejected(bundle):
    path=config(bundle,'app')
    path.write_text(path.read_text().replace('VM_BIND_IP=192.168.56.11','VM_BIND_IP=192.168.56.12'))
    with pytest.raises(ValueError,match='rol'): read_config(path)
    path.write_text(path.read_text()+'EXTRA=$(id)\n')
    with pytest.raises(ValueError,match='interpolación'): read_config(path)


def test_deploy_default_plan_never_calls_docker(bundle,tmp_path):
    fake=tmp_path/'docker'; fake.write_text('#!/bin/sh\nexit 97\n'); fake.chmod(0o755)
    env={**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH']}
    r=subprocess.run([sys.executable,str(ROOT/'despliegue-vm/scripts/vm.py'),'--env-file',str(config(bundle,'app')),'deploy'],env=env,text=True,capture_output=True)
    assert r.returncode==0 and 'PLAN_NO_EJECUTADO' in r.stdout


def test_physical_host_refused_before_docker(bundle,monkeypatch):
    vm=VM(config(bundle,'app')); calls=[]
    def run(command,input=None):
        calls.append(command)
        if command[0]=='systemd-detect-virt': return 'none'
        raise AssertionError('No debe llamar Docker en anfitrión')
    monkeypatch.setattr(vm,'run',run)
    with pytest.raises(ValueError,match='VM reconocida'): vm.deploy()
    assert len(calls)==1


def test_protected_projects_abort_operation(bundle,monkeypatch):
    vm=VM(config(bundle,'app'))
    def run(command,input=None):
        if command[0]=='systemd-detect-virt': return 'oracle'
        if command[0]=='ip': return json.dumps([{'addr_info':[{'local':'192.168.56.11'}]}])
        if command[1]=='context': return json.dumps([{'Endpoints':{'docker':{'Host':'unix:///var/run/docker.sock'}}}])
        if command[1]=='ps': return 'nexustix-distributed-lab'
        raise AssertionError('No debe continuar')
    monkeypatch.delenv('DOCKER_CONTEXT',raising=False); monkeypatch.delenv('DOCKER_HOST',raising=False)
    monkeypatch.setattr(vm,'run',run)
    with pytest.raises(ValueError,match='protegidos'): vm.deploy()


def proof(bundle):
    path=bundle/'fencing.json'
    data={'primary_host':'192.168.56.12','standby_host':'192.168.56.13','primary_vm_uuid':'test-uuid',
          'operator':'operador de prueba','hypervisor_evidence':'poweroff verificado para el test',
          'observed_at':datetime.now(timezone.utc).isoformat(),
          'primary_powered_off':True,'primary_autostart_disabled':True,'application_stopped':True,'accept_async_data_loss':True}
    path.write_text(json.dumps(data)); return path,data


@pytest.mark.parametrize('field',['primary_powered_off','primary_autostart_disabled','application_stopped','accept_async_data_loss'])
def test_fencing_requires_every_confirmation(bundle,field):
    path,data=proof(bundle); data[field]=False; path.write_text(json.dumps(data))
    with pytest.raises(ValueError): validate_fence(path,read_config(config(bundle,'standby')))


def test_fencing_rejects_stale_evidence(bundle):
    path,data=proof(bundle); data['observed_at']=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(); path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='vencido'): validate_fence(path,read_config(config(bundle,'standby')))


def test_partial_promotion_refused_before_any_mutation(bundle,monkeypatch):
    vm=VM(config(bundle,'standby')); path,_=proof(bundle); commands=[]
    monkeypatch.setattr(vm,'guard',lambda:None); monkeypatch.setattr(vm,'validate',lambda:{})
    def sql(service,command):
        commands.append(command)
        assert command=='SELECT pg_is_in_recovery();'
        return 'f'
    monkeypatch.setattr(vm,'sql',sql)
    with pytest.raises(ValueError,match='parcial'): vm.promote(path)
    assert commands==['SELECT pg_is_in_recovery();']


def test_promotion_checks_all_nodes_then_creates_rejoin_slots(bundle,monkeypatch):
    vm=VM(config(bundle,'standby')); path,_=proof(bundle); calls=[]; promoted=set()
    monkeypatch.setattr(vm,'guard',lambda:None); monkeypatch.setattr(vm,'validate',lambda:{})
    def sql(service,command):
        calls.append((service,command))
        if command=='SELECT pg_is_in_recovery();': return 'f' if service in promoted else 't'
        if command=='SELECT pg_promote(true,60);': promoted.add(service); return 't'
        return ''
    monkeypatch.setattr(vm,'sql',sql)
    assert len(vm.promote(path))==3
    assert all(command=='SELECT pg_is_in_recovery();' for _,command in calls[:3])
    assert sum('pg_create_physical_replication_slot' in command for _,command in calls)==3


def test_rejoin_refuses_existing_generation(bundle,monkeypatch):
    path=config(bundle,'rejoin'); path.with_name('primary.FENCED').touch(); vm=VM(path)
    monkeypatch.setattr(vm,'guard',lambda:None)
    monkeypatch.setattr(vm,'validate',lambda:{'volumes':{'data':{'name':'nexustix-vm-rejoin_r1_data'}}})
    def run(command,input=None):
        assert command==['docker','volume','ls','--format','{{.Name}}']
        return 'nexustix-vm-rejoin_r1_data'
    monkeypatch.setattr(vm,'run',run)
    with pytest.raises(ValueError,match='Generación existente'): vm.deploy()


def test_vm_manifests_keep_archive_after_promotion_and_separate_images():
    for role in ('app','primary','standby','rejoin'):
        doc=yaml.safe_load((ROOT/f'despliegue-vm/compose.{role}.yml').read_text())
        for service in doc['services'].values():
            assert service['image'].startswith('nexustix-vm/')
            if role!='app':
                assert 'archive_mode=on' in service['command']
                assert any('/archive' in v for v in service['volumes'])
                assert service['environment']['PGDATA'].endswith('/cluster')
            else: assert not any('_db' in key for key in service.get('depends_on',{}))


def test_entrypoint_keeps_partial_clone_and_refuses_restart(tmp_path):
    bindir=tmp_path/'bin'; bindir.mkdir()
    for name,script in {'chown':'exit 0','su-exec':'shift\nexec "$@"','pg_basebackup':'mkdir -p "$PGDATA"\nprintf 15 > "$PGDATA/PG_VERSION"\nexit 1','docker-entrypoint.sh':'exit 98'}.items():
        p=bindir/name;p.write_text('#!/bin/sh\n'+script+'\n');p.chmod(0o755)
    secret=tmp_path/'password'; secret.write_text('synthetic-password')
    data=tmp_path/'data/cluster'; data.parent.mkdir()
    env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],'PGDATA':str(data),
         'NEXUSTIX_PG_UPSTREAM_HOST':'192.168.56.12','NEXUSTIX_PG_SLOT':'test',
         'NEXUSTIX_REPLICATION_PASSWORD_FILE':str(secret)}
    cmd=['sh',str(ROOT/'despliegue-vm/postgres/entrypoint.sh'),'postgres']
    first=subprocess.run(cmd,env=env,capture_output=True); assert first.returncode==69
    assert (data/'PG_VERSION').read_text()=='15'
    second=subprocess.run(cmd,env=env,capture_output=True); assert second.returncode==65
    assert (data/'PG_VERSION').read_text()=='15'


def test_entrypoint_rejects_fenced_volume(tmp_path):
    (tmp_path/'NEXUSTIX_FENCED').touch()
    r=subprocess.run(['sh',str(ROOT/'despliegue-vm/postgres/entrypoint.sh'),'postgres'],env={**os.environ,'PGDATA':str(tmp_path)},capture_output=True)
    assert r.returncode==64


def test_code_package_excludes_credentials_and_keeps_build_inputs(tmp_path):
    import tarfile
    from empaquetar import pack,allowed
    assert not allowed(Path('despliegue-vm/config.env'))
    assert not allowed(Path('despliegue-vm/secrets/key.pem'))
    assert not allowed(Path('usuarios/test-database-url'))
    target=tmp_path/'code.tar.gz'; pack(target)
    with tarfile.open(target) as archive:
        names=archive.getnames()
        assert 'nexustix/despliegue-vm/postgres/entrypoint.sh' in names
        assert 'nexustix/shared/db_engine.py' in names
        assert all(not name.endswith('.pem') for name in names)
    with pytest.raises(ValueError): pack(target)


def test_firewall_plan_matches_original_destination_and_limits_peers(bundle):
    from firewall import rules
    vm=VM(config(bundle,'standby'))
    policy=rules(vm,'192.168.56.1')
    assert sum(rule[-1]=='DROP' for rule in policy)==3
    assert all('--ctorigdst' in rule for rule in policy[1:-1])
    assert not any('192.168.56.1/32' in rule for rule in policy)
    assert any('192.168.56.11/32' in rule for rule in policy)

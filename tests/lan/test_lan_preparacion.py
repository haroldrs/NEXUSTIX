"""Pruebas locales: no sockets, daemon Docker ni servidores PostgreSQL."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('nexustix_lan',ROOT/'despliegue-pc/lan.py')
lan=importlib.util.module_from_spec(spec);spec.loader.exec_module(lan)


@pytest.fixture
def bundle(tmp_path):
    # IPs solo sintéticas; ningún socket se abre con ellas.
    return lan.prepare(tmp_path/'lan','simulacion',['192.168.80.21','192.168.80.22','192.168.80.23'],lan.PORTS)


def test_database_requires_configuration_and_honors_file(tmp_path,monkeypatch):
    from shared.db_engine import read_database_url
    monkeypatch.delenv('DATABASE_URL',raising=False);monkeypatch.delenv('DATABASE_URL_FILE',raising=False)
    with pytest.raises(RuntimeError,match='Configure'):read_database_url()
    monkeypatch.setenv('DATABASE_URL','sqlite://')
    assert read_database_url()=='sqlite://'
    p=tmp_path/'url';p.write_text('sqlite:///temporary.db\n')
    monkeypatch.setenv('DATABASE_URL_FILE',str(p));assert read_database_url()=='sqlite:///temporary.db'
    p.write_text('')
    with pytest.raises(RuntimeError,match='vacío'):read_database_url()


def test_legacy_secret_mode_requires_external_file_before_creation(tmp_path,monkeypatch):
    import generar_secretos as generator
    import entornos
    monkeypatch.delenv('NEXUSTIX_LEGACY_DB_PASSWORD_FILE',raising=False)
    with pytest.raises(ValueError,match='requiere'):generator.generate(entornos.get('demo'),tmp_path/'new','localhost',True)
    assert not (tmp_path/'new').exists()


def test_generated_bundle_isolated_roles_and_shared_databases(bundle):
    for svc,nodes in {'compras':['pc1','pc2'],'usuarios':['pc2','pc3'],'eventos':['pc2','pc3']}.items():
        values=[(bundle/n/'secrets'/f'{svc}-database-url').read_text() for n in nodes]
        assert len(set(values))==1
        assert '@192.168.80.22:' in values[0] and 'nexustix_app:' in values[0]
    assert not (bundle/'pc1/secrets/jwt-private.pem').exists()
    assert not (bundle/'pc3/secrets/admin-password').exists()
    for p in bundle.rglob('*password'):
        assert len(p.read_text().strip())>=40
        assert p.stat().st_mode & 0o077==0
    original=(bundle/'pc1/config.env').read_bytes()
    with pytest.raises(ValueError):lan.prepare(bundle,'simulacion',['192.168.80.21','192.168.80.22','192.168.80.23'],lan.PORTS)
    assert (bundle/'pc1/config.env').read_bytes()==original


@pytest.mark.parametrize('ips',[['127.0.0.1','192.168.80.22','192.168.80.23'],['192.168.80.21']*3,['8.8.8.8','192.168.80.22','192.168.80.23']])
def test_invalid_topology_rejected_without_creating_files(tmp_path,ips):
    with pytest.raises(ValueError):lan.prepare(tmp_path/'no','simulacion',ips,lan.PORTS)
    assert not (tmp_path/'no').exists()


def test_port_collision_rejected_before_generation(tmp_path):
    ports={**lan.PORTS,'COMPRAS':lan.PORTS['USUARIOS']}
    with pytest.raises(ValueError,match='Puertos'):lan.prepare(tmp_path/'no','simulacion',['192.168.80.21','192.168.80.22','192.168.80.23'],ports)
    assert not (tmp_path/'no').exists()


def test_remote_gateway_uses_every_lan_peer_and_active_database_health(bundle):
    cfg=lan.Node(bundle/'pc1/config.env').cfg
    rendered=lan.haproxy_config(cfg)
    for endpoint in ['192.168.80.21:8003','192.168.80.22:8003','192.168.80.22:8001','192.168.80.23:8001','192.168.80.22:8002','192.168.80.23:8002']:
        assert endpoint in rendered
    assert rendered.count('http-check expect status 200')==3
    assert 'retries 0' in rendered  # no replay automático de POST tras respuestas ambiguas
    for role in lan.ROLES:
        doc=yaml.safe_load((ROOT/f'despliegue-pc/compose.{role}.yml').read_text())
        assert set(doc['services'])==lan.ROLES[role]
        assert sum(x.endswith('_db') for x in doc['services'])==(3 if role=='pc2' else 0)
        assert not any('replica' in x for x in doc['services'])
        if 'compras' in doc['services']:
            assert 'LAN_PC1_IP' in doc['services']['compras']['environment']['EVENTOS_URL']


def test_protected_host_rejected_before_compose_or_mutation(bundle,monkeypatch):
    node=lan.Node(bundle/'pc1/config.env');calls=[]
    monkeypatch.delenv('DOCKER_HOST',raising=False);monkeypatch.delenv('DOCKER_CONTEXT',raising=False)
    def run(args,**kwargs):
        calls.append(args)
        if args[0]=='ip':return SimpleNamespace(stdout=json.dumps([{'addr_info':[{'local':'192.168.80.21'}]}]))
        assert 'ps' in args
        return SimpleNamespace(stdout=json.dumps({'Labels':'com.docker.compose.project=nexustix-distributed-lab'}))
    monkeypatch.setattr(lan,'run',run)
    with pytest.raises(ValueError,match='protegido'):node.stop_purchase()
    assert len(calls)==2


def test_remote_daemon_and_wrong_role_refused(bundle,monkeypatch):
    node=lan.Node(bundle/'pc1/config.env');monkeypatch.setenv('DOCKER_HOST','tcp://untrusted:2375')
    monkeypatch.setattr(lan,'run',lambda *a,**k:pytest.fail('No llamar comandos'))
    with pytest.raises(ValueError,match='DOCKER'):node.guard()
    with pytest.raises(ValueError,match='PC 1'):lan.Node(bundle/'pc2/config.env').stop_purchase()


class FakeNode:
    role='pc1'
    cfg={'LAN_PC1_IP':'192.168.80.21','LAN_PC2_IP':'192.168.80.22','LAN_COMPRAS_PORT':'8003','LAN_BALANCER_PORT':'8005'}
    def __init__(self):self.stopped=False;self.history=[]
    def guard(self):self.history.append('guard')
    def stop_purchase(self):self.stopped=True;self.history.append('stop')
    def start_purchase(self):self.stopped=False;self.history.append('start')


def test_failover_simulation_routes_five_checks_to_pc2_and_restores():
    node=FakeNode();observed=[]
    def read(url,*args):
        replica='2' if '192.168.80.22' in url or node.stopped else '1'
        observed.append((url,replica))
        return {'status':'ok','instancia':replica}
    result=lan.failover(node,read=read,sleep=lambda *_:None)
    assert result['solicitudes_pc2']==5 and result['pc1_recuperada']
    assert node.history==['guard','stop','start']
    assert sum(':8005/health' in url and replica=='2' for url,replica in observed)==6


def test_failover_restores_pc1_even_when_authenticated_read_fails():
    node=FakeNode()
    def read(url,*args):
        if url.endswith('/ordenes/mias'):
            if node.stopped:raise RuntimeError('Fallo simulado')
            return []
        return {'status':'ok','instancia':'2' if node.stopped or '192.168.80.22' in url else '1'}
    with pytest.raises(RuntimeError,match='simulado'):lan.failover(node,token='synthetic',read=read,sleep=lambda *_:None)
    assert node.history==['guard','stop','start'] and not node.stopped


def test_scan_review_does_not_approve_changed_line_or_other_file():
    from scripts.revisar_publicacion import fingerprint
    a=fingerprint('tests/a.py','literal','example')
    assert a!=fingerprint('tests/a.py','literal','changed')
    assert a!=fingerprint('app.py','literal','example')

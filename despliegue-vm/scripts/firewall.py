#!/usr/bin/env python3
"""Política de puertos Docker en el invitado. Plan por defecto; no toca el anfitrión."""
import argparse
import ipaddress
import json
import subprocess
from vm import VM


def rules(vm,admin):
    ip=ipaddress.ip_address(admin)
    if ip.version!=4 or not ip.is_private or ip.is_loopback: raise ValueError('Administrador debe tener IPv4 privada')
    ports=(8080,8001,8002,8003,8004,8005) if vm.role=='app' else (5432,5433,5434)
    allowed={str(ip)} if vm.role=='app' else {vm.cfg['VM_APP_HOST'],vm.cfg['VM_PRIMARY_HOST'],vm.cfg['VM_STANDBY_HOST']}
    result=[['-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','RETURN']]
    for port in ports:
        match=['-p','tcp','-m','conntrack','--ctorigdst',vm.cfg['VM_BIND_IP'],'--ctorigdstport',str(port)]
        for host in sorted(allowed): result.append([*match,'-s',host+'/32','-j','RETURN'])
        result.append([*match,'-j','DROP'])
    result.append(['-j','RETURN'])
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',required=True);p.add_argument('--admin-ip',required=True)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args(); vm=VM(a.env_file); policy=rules(vm,a.admin_ip)
    chain='NXVM_'+vm.role.upper()
    commands=[['iptables','-N',chain]]+[['iptables','-A',chain,*r] for r in policy]+[['iptables','-I','DOCKER-USER','1','-j',chain]]
    if not a.apply:
        print(json.dumps({'status':'PLAN_NO_EJECUTADO','backend':'iptables','commands':commands},indent=2)); return
    vm.guard()
    # Docker nftables no tiene DOCKER-USER: fallar, nunca alterar backend del daemon.
    vm.run(['iptables','-S','DOCKER-USER'])
    # No modificar cadenas ya existentes: conservar política y exigir revisión.
    found=subprocess.run(['iptables','-S',chain],capture_output=True).returncode==0
    if found: raise SystemExit('Cadena existente: se conserva; verificar reglas y persistencia manualmente')
    for cmd in commands: vm.run(cmd)
    print('Política activa en ESTA VM; no persistida. Consultar RED.md antes de reiniciar/firewalld reload.')


if __name__=='__main__': main()

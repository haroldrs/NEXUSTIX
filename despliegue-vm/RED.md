# Comunicación y control de acceso

**No aplicado al host ni a VMs en esta fase.** IPs propuestas de ejemplo;
comprobar con `ip route`/inventario del hipervisor que 192.168.56.0/24 no
colisione con redes existentes antes de crearla.

Cada VM: adaptador 1 NAT (instalación/actualizaciones) y adaptador 2 host-only
privado. Conservar la ruta por defecto de NAT; la interfaz privada no tiene
gateway ni cambia DNS. `scripts/configurar_red.py` genera un plan y solo aplica
con `--apply`, dentro de una VM, con MAC exacta y sin tocar la interfaz de la
ruta por defecto. El script **no crea redes VirtualBox** ni modifica interfaces
del Fedora anfitrión. Si las VMs ya tienen red correcta, no ejecutarlo.

| Origen | Destino / puerto | Uso |
|---|---|---|
| Equipo de administración .1 | app .11:8080,8001,8002,8005 | Web, autenticación, catálogo, pedidos |
| Equipo de administración .1 | app .11:8003,8004 | Diagnóstico directo de réplicas Compras |
| app .11 | primary .12 / standby .13:5432,5433,5434 | PostgreSQL libpq |
| standby .13 | primary .12:5432,5433,5434 | pg_basebackup y WAL |
| antigua primary .12 | promovida .13:5432,5433,5434 | Reintegración y WAL en sentido inverso |
| Administrador | cada VM:22 | SSH autorizado, si se habilitó en la imagen invitada |

Puerto externo 5433/5434 se traduce al 5432 del contenedor correspondiente.
Las IP internas Docker pueden repetirse entre hosts: no usarlas como endpoint
remoto ni para identificar el primario físico. La configuración pública de la
web usa .11; las URLs PostgreSQL permanecen privadas en archivos de secretos.

## Firewall del invitado

Publicar en `VM_BIND_IP` evita enlazar deliberadamente con la interfaz NAT,
pero **no sustituye filtrado de origen**. Docker añade sus propias reglas de
forwarding; abrir/cerrar puertos solo en la zona habitual de firewalld no basta
para describir el acceso a contenedores publicados. Véase la documentación de
[Docker sobre filtrado](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
y [DOCKER-USER con iptables](https://docs.docker.com/engine/network/firewall-iptables/).

`scripts/firewall.py` prepara una cadena por rol en DOCKER-USER, limitada al
IP/puerto original con conntrack, permite los orígenes de la tabla y rechaza
los demás para esos puertos. Conserva conexiones establecidas y tráfico ajeno.
El acceso SSH corresponde a INPUT/firewalld del invitado y debe permanecer
habilitado para el administrador antes de operar remotamente.

```bash
# Dentro de la VM; primero sin --apply para revisar:
sudo python3 despliegue-vm/scripts/firewall.py \
  --env-file /etc/nexustix-vm/config.env --admin-ip 192.168.56.1
# Aplicar únicamente con autorización sobre este invitado:
sudo python3 despliegue-vm/scripts/firewall.py \
  --env-file /etc/nexustix-vm/config.env --admin-ip 192.168.56.1 --apply
```

Requiere backend Docker iptables (incluido iptables-nft). Si DOCKER-USER no
existe, falla sin cambiar el backend. No se deshabilitan reglas de Docker,
SELinux ni firewalld. Si ya existe la cadena del rol, el script se detiene para
preservarla: comprobarla antes de reemplazar nada.

Las reglas preparadas **no se instalan como servicio persistente**. Reiniciar
Docker/firewalld/VM exige revisar y restablecer la política desde consola antes
de reabrir acceso. Esta persistencia queda PENDIENTE de validar en la imagen
Fedora elegida; no se ofrece protección garantizada durante ese intervalo.
No exponer el segmento de demostración a redes no confiables.

Como segunda barrera, el `pg_hba.conf` inicial de las VMs acepta SCRAM desde
app y los dos nodos de base; replicación solo entre .12/.13. Se aplica al crear
clústeres nuevos. Las bases ya existentes no se reescriben para adaptar reglas.

## Comprobaciones antes de iniciar la aplicación

1. En cada VM: `ip -4 address`, `ip route`, `timedatectl` y `chronyc tracking`.
2. En primary/standby: `vm.py … diagnose`; verificar rol, streaming, puertos de
   upstream, estado de slots y fallos del archivador.
3. Desde VM app: `verificar.py … --expected-primary primary --output NUEVO.json`.
   Conecta con las credenciales reales desde los contenedores, sin publicarlas.
4. Desde el administrador: abrir el portal y probar login/catálogo. Desde otro
   origen no autorizado, comprobar que los puertos publicados están bloqueados.
5. Confirmar desde una interfaz ajena a la privada que PostgreSQL no sea accesible.
   Un `compose config` válido no acredita ninguna de estas comprobaciones.

Cambiar direcciones requiere regenerar un paquete VM nuevo antes del despliegue;
no editar secretos del laboratorio. Para perder conectividad durante un ensayo,
seguir FALLOS.md: desconectar únicamente el adaptador privado de la VM elegida
desde el hipervisor, con su consola disponible y sin cambiar firewall del host.

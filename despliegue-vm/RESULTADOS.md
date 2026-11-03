# Resultado de preparación VM — 03/11/2026

**Preparación terminada; despliegue y ensayos entre VMs pendientes.** No se
recibieron IPs/SSH confirmados de VMs operativas. Se usaron .11/.12/.13 como
parámetros de ejemplo y solo se generaron secretos sintéticos en `/tmp` para
validar manifiestos. Esos secretos no son una instalación ni deben reutilizarse
para las VMs definitivas.

La línea base [RESULTADOS_DISTRIBUIDO.md](../docs/RESULTADOS_DISTRIBUIDO.md)
permanece idéntica. No se repitieron ni reimplementaron sus pruebas funcionales.
No se arrancaron, detuvieron ni recrearon contenedores de laboratorio, staging
o demo. La lectura final de Docker mostró 12 contenedores saludables del
laboratorio y 13 de staging, con sus tiempos de actividad anteriores.

## Cambios entregados

| Área | Archivos | Resultado |
|---|---|---|
| Topología | `compose.app.yml`, `compose.primary.yml`, `compose.standby.yml`, `compose.rejoin.yml`; `../scripts/generar_topologia.py --only-vm` | Imágenes VM separadas, archivado WAL tras promoción, upstream/puertos por base, volúmenes nuevos por generación |
| Arranque PostgreSQL | `postgres/Dockerfile`, `postgres/entrypoint.sh`, `postgres/init-access.sh` | No elimina clones parciales; marcador persistente y fencing; HBA SCRAM limitado a IPs del despliegue nuevo |
| Preparación | `scripts/preparar.py`, `scripts/empaquetar.py`, `config.env.example` | Artefacto de código y secretos mínimos por rol; rechaza destinos existentes |
| Invitado | `scripts/instalar.py`, `scripts/configurar_red.py`, `scripts/firewall.py` | Plan por defecto; aplicación explícita solo en invitado verificado. Ninguno se aplicó |
| Operación | `scripts/vm.py`, `fencing.example.json` | Guardas de VM/IP/Docker/proyectos; diagnóstico, deploy, stop/start, retiro y promoción controlada |
| Aceptación | `scripts/verificar.py`, `scripts/aceptacion.py` | Roles PostgreSQL desde app, observación HTTP, replay y compra nueva antes/después de conmutación |
| Pruebas | `../tests/vm/test_preparacion.py` | Guardas, secretos, fencing, reintegración, clones parciales y empaquetado con dobles de ejecución |
| Guías | `README.md`, `RED.md`, `FALLOS.md`, actualización de `RECUPERACION.md` y README raíz | Requisitos, secuencia operativa, matriz de fallos y límites explícitos |

No cambió código de Usuarios, Eventos, Compras o frontend; tampoco sus modelos,
migraciones, APIs o datos. El generador general cambia para soportar preparación
VM, pero no se volvió a escribir el Compose del laboratorio.

## Validaciones ejecutadas

| Validación | Acción real | Obtenido | Estado |
|---|---|---|---|
| Pruebas nuevas y aislamiento | Pytest `tests/vm tests/test_distribuido_config.py` | **25 aprobadas**, 0 fallidas, 9.27 s | APROBADA |
| Compose app | Renderizar config con archivos de secretos sintéticos | 6 servicios, sin DB local | APROBADA como configuración |
| Compose primary/standby/rejoin | Renderizar los tres manifiestos | 3 servicios por manifiesto, puertos externos distintos por base | APROBADA como configuración |
| Imagen PostgreSQL VM | `docker build --tag nexustix-vm/postgres:15 --file despliegue-vm/postgres/Dockerfile .` | Construcción exitosa; imagen nueva, sin arrancar contenedor | APROBADA como build |
| Python / shell | compileall y `sh -n` | Sin errores de sintaxis | APROBADA |
| Preservación de archivos | SHA256 contra registro previo | Compose demo, staging, laboratorio e informe base idénticos | APROBADA |
| Preservación de datos | No se ejecutó ningún comando sobre bases/volúmenes existentes | Sin borrado, reinicialización ni migración de entornos actuales | Cumplida por alcance de acciones |
| Streaming/fencing/conmutación VM | No ejecutado | Sin VMs/accesos confirmados | PENDIENTE |
| Instalación/red/firewall/SELinux del invitado | Solo scripts y guías preparados | Ningún cambio aplicado | PENDIENTE |
| Persistencia de firewall tras reboot | No implementada como servicio | Revisión manual requerida después de reinicios | PENDIENTE |

La construcción de imagen tuvo un primer intento cuyo control automático de
permisos agotó su plazo; no se ejecutó entonces. El único reintento fue autorizado
y terminó correctamente. No quedó una aprobación bloqueante pendiente.

Evidencia completa de Pytest en
`resultados/preparacion-vm-20261103T170114Z/pytest.{log,xml}`. Resumen portable:
[20261103_preparacion_vm.json](../docs/evidencias/20261103_preparacion_vm.json).
Las pruebas de promoción usan un adaptador de comandos; acreditan guardas y
secuencia, **no una promoción física entre VMs**. La mecánica PostgreSQL real
ya fue validada localmente en la línea base, que no se reetiqueta como prueba VM.

## Respaldo previo y límites

Antes de editar: `/tmp/nexustix-antes-fase-vm-20261103T132000Z.tar.gz`;
SHA256 `2cb744eceff1b4f2614b627627c83bc920a2ed9c77af31327e6fabefef1245fb`.
Incluye configuración/guías VM anteriores, generadores, prueba de configuración,
README y gitignore. No fue necesario alterar ni recuperar datos existentes.

Los scripts se prepararon para el primer failover .12 → .13. No hacen elecciones
automáticas, no verifican por sí solos la veracidad del registro de fencing,
no garantizan RPO cero y no automatizan failback. Instancias sobre un único
host físico comparten energía, almacenamiento y recursos. Una sola VM app no
tolera su propia caída. El firewall preparado requiere backend iptables y
revisión de persistencia en el Fedora invitado elegido.

Próximo paso operativo: preparar tres VMs reales, confirmar IPs/MAC/SSH y acceso
al hipervisor, y ejecutar la secuencia de README con su evidencia. Crear/modificar
la red del anfitrión requiere autorización específica; no está incluido entre
las acciones realizadas.

## Referencias utilizadas

- [Docker Engine en Fedora](https://docs.docker.com/engine/install/fedora/): repositorio DNF e instalación.
- [Docker y firewall](https://docs.docker.com/engine/network/packet-filtering-firewalls/), [DOCKER-USER](https://docs.docker.com/engine/network/firewall-iptables/): forwarding y filtrado de puertos publicados.
- [PostgreSQL 15: libpq](https://www.postgresql.org/docs/15/libpq-connect.html): candidatos y selección de sesión escribible.
- [PostgreSQL 15: standby](https://www.postgresql.org/docs/15/warm-standby.html), [pg_basebackup](https://www.postgresql.org/docs/15/app-pgbasebackup.html): streaming y conservación de clones fallidos.

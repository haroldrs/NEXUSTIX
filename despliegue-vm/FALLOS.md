# Fallos, fencing, promoción y reintegración

**Pruebas preparadas, no ejecutadas entre VMs.** No usar `scripts/db_ha.py pruebas`:
actúa sobre staging. Tampoco ejecutar la suite histórica `tests/resiliencia`.
Los scripts de esta carpeta restringen proyectos y roles de VMs.

## Precondiciones y evidencia

Tres VMs operativas con hora sincronizada, diagnósticos guardados, copia de
secretos y backup verificable fuera de los discos que se van a detener.
Registrar IP/UUID de cada VM, versión de imágenes, operador, hora UTC inicial,
acción, esperado, obtenido y estado APROBADA/FALLIDA/BLOQUEADA/PENDIENTE.
Guardar JSON nuevos por ejecución; no sobrescribir evidencias anteriores.

Antes de inyectar fallos ejecutar `aceptacion.py --stage before … --apply` desde
app (README). Conservar su archivo `--state` en almacenamiento persistente; los
paths `/tmp` de ejemplos son solo para ensayos sin reiniciar esa VM. Verificar
que el standby haya reproducido los commits de la compra. `lag_bytes=0` observado
antes del fallo no garantiza RPO cero para escrituras posteriores.

Un observador puede ejecutarse desde el equipo administrador autorizado, usando
una copia de `config.env` (sin secretos). Solo hace GET de health, sin Docker:

```bash
python3 despliegue-vm/scripts/verificar.py --env-file config.env \
  --observe-seconds 120 --output /ruta/nueva/observacion.json
```

HTTP 0 significa que no hubo respuesta válida en 2 s: no demuestra por sí solo
una caída del proceso. El observador conserva muestras y no las marca APROBADAS
automáticamente. Correlacionarlas con hipervisor, logs y diagnóstico SQL.

## Promoción manual de .13

1. Detener aplicación con `vm.py … stop --apply` dentro de app; así se detienen
   también sus workers. No aceptar nuevas ventas durante conmutación conjunta.
2. En el hipervisor, identificar UUID de **la VM primary .12**. Apagarla/aislarla
   de forma verificable y desactivar su autoarranque. Un ping/SSH fallido o
   desconectar solo un cable no demuestra que haya dejado de ser primario.
   Guardar la salida de `VBoxManage showvminfo UUID --machinereadable` o evidencia
   equivalente y hora. La acción de apagado debe ejecutarla el operador; estos
   scripts no controlan VirtualBox ni modifican la red anfitriona.
3. Copiar `fencing.example.json` a un archivo privado nuevo. Completar UUID,
   operador, referencia de evidencia, IPs, hora ISO8601 con zona y verificaciones
   explícitas. Aceptar el posible RPO de replicación asíncrona. El registro vence
   después de una hora. **Es una declaración del operador, no una comprobación
   criptográfica ni un mecanismo automático de fencing.**
4. En standby, guardar `vm.py … diagnose`. Comprobar replay y ausencia de errores
   de almacenamiento; registrar WAL que pudiera faltar. Luego:

```bash
sudo python3 despliegue-vm/scripts/vm.py --env-file /etc/nexustix-vm/config.env \
  promote --fence-record /ruta/fencing.json
# Tras revisar plan y evidencia:
sudo python3 despliegue-vm/scripts/vm.py --env-file /etc/nexustix-vm/config.env \
  promote --fence-record /ruta/fencing.json --apply
```

Las tres bases deben estar inicialmente en recovery; se promueven, se elimina
la espera síncrona a un nodo inexistente y se crean slots `*_db_rejoin`. Si una
promoción queda parcial, el script falla: mantener app detenida y diagnosticar;
no repetir a ciegas ni reinicializar nada. Es una conmutación manual entre tres
clústeres, no una transacción atómica global.

5. Confirmar recovery=false y archive_mode=on en las tres bases promovidas.
   Revisar `failed_count`, espacio y slots: un slot inactivo puede retener WAL.
   Se limita a 1024 MB; si se invalida por exceder el límite, hay que clonar una
   generación nueva, no asumir que la réplica puede continuar.
6. En app: `vm.py … start --apply`; verificar salud y nuevo nodo:

```bash
sudo python3 despliegue-vm/scripts/verificar.py --env-file /etc/nexustix-vm/config.env \
  --expected-primary standby --output /ruta/nueva/conexiones-promovida.json
sudo python3 despliegue-vm/scripts/aceptacion.py --env-file /etc/nexustix-vm/config.env \
  --stage after --expected-primary standby --state /ruta/vm-compra.json \
  --output /ruta/nueva/despues.json --apply
```

No cambiar URLs, código ni invertir `VM_PRIMARY_HOST`/`VM_STANDBY_HOST`.
libpq prueba ambos endpoints y exige sesión escribible. La prueba repite la
compra original con su misma clave y pago, comprueba que no duplique QR y crea
una compra nueva; verifica stock. Los endpoints normales utilizan los pools
reales de la aplicación. El ensayo incluye detener/arrancar app, por lo que
no acredita continuidad del mismo proceso durante el cambio. Esa reconexión
sin reinicio ya tiene evidencia local en la línea base del 03/11.

## Reintegración de la antigua VM primary .12

No volver a conectar su NIC privada antes de retirar el primario antiguo.
Bootearla desde consola **con el adaptador privado desconectado** y autoarranque
de VM deshabilitado. Si Docker arranca contenedores antiguos, quedan aislados
de los otros nodos; no abrir su red ni aceptar ventas.

1. En la VM .12, usar su configuración original de rol primary:

```bash
sudo python3 despliegue-vm/scripts/vm.py --env-file /etc/nexustix-vm/config.env retire --apply
```

Retira el auto-restart de esos tres contenedores, los detiene, escribe
`NEXUSTIX_FENCED` en sus PGDATA y `primary.FENCED` junto al config. No borra ni
reutiliza esos volúmenes. Solo `retire` permite una NIC desconectada sin IP local;
las demás operaciones exigen IP/rol correctos. Si falla algún paso, no conectar
la red hasta resolverlo.

2. Copiar el `config.env` del paquete **rejoin** a
   `/etc/nexustix-vm/rejoin.env`, sin sustituir el original; mismos secretos de
   bases. Elegir `VM_REJOIN_GENERATION=r1` que nunca se haya usado.
3. Conectar la interfaz privada ya retirada; verificar que el primario antiguo
   no se reinició. En .13 confirmar slots de reintegración presentes e inactivos.
4. En .12:

```bash
sudo python3 despliegue-vm/scripts/vm.py --env-file /etc/nexustix-vm/rejoin.env deploy --apply
sudo python3 despliegue-vm/scripts/vm.py --env-file /etc/nexustix-vm/rejoin.env diagnose
```

Usa proyecto `nexustix-vm-rejoin`, nombres de volumen nuevos y clone desde .13.
Si una generación ya existe, `deploy` la rechaza. `start` permite reanudar una
copia válida existente, pero el entrypoint bloqueará una copia parcial. En ese
caso conservarla y escoger otra generación; no ejecutar `rm`, prune o reset.

5. Verificar recovery=true en .12, receiver desde .13, streaming en .13, avance
   de LSN, rechazo de escrituras en la réplica y consulta de la compra antes y
   después del fallo. Ejecutar `verificar.py --expected-primary standby` en app:
   debe ver .13 escribible y .12 en recuperación.
6. Tomar backup nuevo. No hacer failback automático. Cualquier conmutación
   inversa requiere otro procedimiento revisado y fencing del nuevo primario;
   el script actual prepara la primera conmutación .12 → .13.

## Matriz preparada para ejecución real

| Escenario | Inicial | Acción del operador | Esperado / criterio | Estado actual |
|---|---|---|---|---|
| Caída VM app | Compra baseline completa, DB streaming | Apagar app desde hipervisor, observar, volver a arrancar | Web/APIs indisponibles durante caída; salud y pedidos recuperados después. No se espera HA de esa VM | PENDIENTE |
| Pérdida red app→DB | VMs saludables | Desconectar NIC privada de app; observar; reconectar | Errores temporales, sin sobreventa; misma clave recupera intención tras reconexión | PENDIENTE |
| Pérdida red primary↔standby | Primario vivo | Desconectar NIC privada de standby y restaurarla antes de agotar WAL | Primario continúa en asíncrono; réplica reconecta. No promover por este síntoma | PENDIENTE |
| Caída VM primary | Compra replicada | Apagar .12, detener app, verificar fencing, promover .13 | Solo .13 escribible, compra previa y nueva válidas; registrar cualquier pérdida si hubo lag | PENDIENTE |
| Primario inaccesible pero no aislado | Partición de red | Mantener fencing sin confirmar | Script rechaza promoción; no crear dos primarios | PENDIENTE entre VMs; rechazo del registro probado localmente |
| Reintegración | .13 primario; .12 retirada | Clonar a volúmenes nuevos | .12 solo lectura, streaming inverso, compras conservadas | PENDIENTE |
| Reinicio standby promovido | .13 escribible | Reiniciar solo esa VM después del backup | Sigue siendo primario, archivado WAL operativo, app reconecta | PENDIENTE |
| Caída equipo físico | Todas las VMs en él | No ejecutar durante esta fase | Toda la plataforma cae; no hay tolerancia al fallo físico compartido | PENDIENTE / limitación arquitectónica |

Para pérdida de red conservar consola del hipervisor y un procedimiento de
reconexión. No introducir reglas temporales de DROP en Fedora anfitrión.
No marcar APROBADA por observar solo health: completar comprobaciones de compra,
QR, inventario y roles de base. Si no hay VMs/acceso, conservar PENDIENTE.

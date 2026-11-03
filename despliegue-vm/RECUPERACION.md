# Backups, WAL y recuperación

Estado VM: preparado, no ejecutado. Promoción/fencing/reintegración y comandos
por rol: [FALLOS.md](FALLOS.md). Evidencia de la preparación: [RESULTADOS.md](RESULTADOS.md).

Cada primario nuevo archiva WAL en su volumen `<proyecto>_<base>_wal` con
`archive_timeout=60s`, checkpoints PostgreSQL cada 5 minutos y WAL de streaming.
`archive-wal.sh` publica segmentos completos sin sobreescribir contenido
diferente. `/diagnostico/db` informa fallos del archivador y retraso por réplica.
Verificarlo después de cada arranque. Un volumen en el mismo host no es copia
externa: copiar backups/WAL a almacenamiento independiente.

## Respaldo físico y PITR

Ejecutar en un host autorizado con herramientas de la **misma versión mayor**
del servidor. Conexión por `PGHOST`, `PGPORT`, `PGUSER` y `PGPASSFILE` 0600,
sin contraseñas en argumentos ni historial. El usuario necesita permiso de
replicación para `pg_basebackup`. Si no se autoriza esa conexión desde un host
de backup, ejecutar dentro de la VM de base de datos.

```bash
python3 scripts/physical_backup.py --pg-bin /ruta/postgresql/bin backup /backups/nueva-copia
# En el servidor, después de una operación que se quiera conservar:
# SELECT pg_create_restore_point('antes_del_cambio'); SELECT pg_switch_wal();
# Esperar que el WAL esté archivado; copiar backup y todos los WAL posteriores.
python3 scripts/physical_backup.py --pg-bin /ruta/postgresql/bin restore \
  /backups/nueva-copia/base /restauraciones/directorio-nuevo \
  --archive /backups/wal --target-name antes_del_cambio
```

Ambos comandos rechazan destinos existentes. `pg_verifybackup` verifica el
manifiesto SHA256 antes de preparar la restauración. El segundo comando
**no arranca** PostgreSQL: crea `recovery.signal` y configuración de recuperación
hasta el punto nombrado, con promoción al alcanzarlo. Arrancarlo primero en
loopback con un puerto y socket exclusivos, usando `pg_ctl -D ... -o '-c
listen_addresses=127.0.0.1 -c unix_socket_directories=/ruta/nueva -p PUERTO'`.
Si falta WAL o el destino no se alcanza, PostgreSQL debe fallar; no borrar la
señal para saltarse la recuperación.

`tests/postgres/test_pitr.py` ejecuta este procedimiento realmente: restaura
filas 1 y 2, excluye la fila 3 escrita después del punto, y verifica que el
servidor original no se sobrescribe. Un backup de un clúster no es un snapshot
global de las tres bases. Para recuperación conjunta, detener escritores
(incluido Eventos y los workers de Compras), drenar Sagas, marcar puntos en
las tres bases y conservar los tres backups/WAL. Después, auditar y reconciliar.

## Frecuencia, retención y comprobaciones

- Backup físico completo diario por clúster, fuera de horario de demostración.
- Archivado continuo de WAL; comprobar `failed_count` y espacio libre.
- Conservar al menos 7 backups diarios **verificados** y los WAL desde el
  inicio del backup más antiguo retenido, incluidas las líneas de tiempo.
- Verificación `pg_verifybackup` después de cada backup y ensayo de PITR
  semanal en otro directorio. Un hash correcto no acredita WAL completo.
- La eliminación por retención es manual y solo después de comprobar otro
  respaldo restaurable; no hay tarea que borre automáticamente datos.
- Plantillas `nexustix-backup@.service`/`.timer` y `backup.env.example` preparadas:
  ejecutan `scripts/backup_programado.py` diariamente, verifican cada copia y
  generan una lista de revisión de retención sin eliminar archivos. En la VM
  futura crear usuario de sistema `nexustix-backup`, instalar el código en
  `/opt/nexustix`, preparar `/var/lib/nexustix/backups` de su propiedad y archivos
  `/etc/nexustix/backup-{usuarios,eventos,compras}.env`. Ajustar puerto/directorio
  por base (5432/5433/5434); proporcionar `.pgpass` 0600 y acceso SCRAM limitado.
  Solo después instalar las unidades en `/etc/systemd/system`, `daemon-reload`
  y activar las tres instancias del timer. No se instaló ningún timer en Fedora
  ni en VMs; programación preparada, ejecución programada pendiente de validar.

RPO asíncrono: WAL no replicado o no archivado puede perderse. `archive_timeout`
no garantiza por sí solo un RPO de 60 segundos: fallos del destino/archivador
pueden ampliarlo. RTO depende de decisión, tamaño del backup y replay.

Rollback revierte una transacción local; una compensación devuelve inventario
con otra transacción; PITR retrocede un clúster; recuperación de Saga avanza o
compensa pasos persistidos. Son mecanismos distintos.

# PostgreSQL multinodo: recuperación operada y sin split-brain

Este procedimiento pertenece exclusivamente a `despliegue-nodos`. Los comandos
son instrucciones para el operador; no se ejecutan al generar el inventario.
No se debe apuntar este despliegue a volúmenes de local, staging, laboratorio,
VM o LAN antiguos. Los volúmenes de datos, estado y WAL incluyen una generación
por base y nodo; ninguna herramienta de esta carpeta borra una generación.

## Garantías y límites

Cada base lógica tiene un único primario declarado y cero o más standbys.
Las aplicaciones usan URLs libpq multi-host con
`target_session_attrs=read-write`; una conexión nueva busca un servidor que
acepte escrituras. Las conexiones abiertas y operaciones en curso pueden fallar
durante una conmutación. Esto no identifica un primario legítimo si se permiten
dos: **el fencing previo es obligatorio**.

No hay elección automática, coordinador distribuido ni quórum de consenso.
`ANY 1 (...)` en PostgreSQL significa confirmación de WAL por una réplica; no
es un mecanismo de elección de líder ni prueba de aislamiento del anterior.
El perfil de alta disponibilidad prepara componentes redundantes y recuperación
manual: no garantiza continuidad automática, RTO determinado ni RPO cero.
Si no puede probarse el aislamiento del primario anterior, no se promueve otro.

La evaluación de Patroni, consenso y fencing para Fedora/Windows WSL está en
[Coordinación y acceso estable](COORDINACION.md). Su integración automática
permanece bloqueada por garantías físicas e infraestructura todavía no
disponibles. El Centro de Control sólo observa; no ejecuta este procedimiento.

Con replicación asíncrona pueden perderse commits confirmados que no llegaron
a una réplica. Con replicación síncrona las escrituras esperan las confirmaciones
configuradas; si faltan los standbys exigidos, quedan bloqueadas. La promoción
de una réplica arbitraria tampoco permite prometer RPO cero: hay que verificar
qué copias recibieron los commits y seleccionar una que los conserve.

La cuenta `nexustix_app` no es superusuario y es propietaria de su base/esquema
para las migraciones de la aplicación. Tiene `pg_monitor` para observar los
canales WAL y estadísticas, no para promover servidores. `postgres` sólo entra por el socket del
contenedor. `replicator` y la aplicación usan SCRAM y CIDRs explícitos del
inventario; este despliegue no agrega cifrado TLS a la LAN. Los secretos se
mantienen fuera del repositorio y se copian a archivos privados del volumen de
estado para que PostgreSQL pueda leerlos sin depender del propietario del bind
mount. No publique ni respalde ese volumen como si fuera configuración pública.

## Arranque y ampliación

En un clúster nuevo, arranque primero las bases primarias, después sus standbys
y finalmente los servicios. El primario crea los slots físicos definidos en
el inventario exclusivamente durante `initdb`. Las consultas de esa
inicialización usan commit local para permitir crear la primera réplica; las
conexiones posteriores de las aplicaciones usan la política síncrona elegida.

Modificar el inventario no crea slots en un primario ya inicializado. Después
de regenerar y actualizar la configuración de ese primario, prepare sus slots
antes de arrancar un nuevo standby. Ejemplo en el nodo que realmente es primario
de Compras, desde su directorio de configuración generado:

```bash
docker compose exec compras_db nexustix-pg-manage prepare-slots
```

Repita para `usuarios_db` y `eventos_db` si también cambió su asignación.
Este comando no cambia roles ni promociona; sólo crea slots ausentes del
inventario, y rechaza un servidor en recuperación. Si añadió direcciones o
cambió el puente Docker, actualice primero la configuración del primario: sus
reglas de acceso se regeneran al arrancar esta imagen.

Un fallo durante `pg_basebackup` conserva la copia parcial y
`NEXUSTIX_CLONE_PENDING`. Un reinicio no vuelve a clonar sobre ella. Asigne una
generación nueva a **esa base en ese nodo**, regenere y conserve los volúmenes
anteriores para diagnóstico. Nunca retire el marcador para reutilizar una
copia parcial.

## Estado previo a una decisión

En cada nodo alcanzable con una copia de la base, el operador puede consultar:

```bash
docker compose exec compras_db nexustix-pg-manage status
```

La salida muestra identificador de sistema, recuperación, timeline, LSN
recibido/aplicado, conexiones de replicación y slots. Sólo compare LSN de copias
del mismo sistema y de una historia de timelines compatible. La ausencia de
respuesta de una máquina no demuestra que esté apagada: puede estar aislada de
esta parte de la LAN y seguir aceptando clientes en otra.

Con el primario accesible, pause las escrituras de aplicación de esa base y los
procesos que puedan escribir en ella, incluidos coordinadores de Compras y
escrituras internas. Poner únicamente el frontend en mantenimiento no basta.
Registre el WAL final del primario después de ese bloqueo:

```bash
docker compose exec compras_db psql -X -U postgres -d compras_db \
  -c 'SELECT system_identifier::text FROM pg_control_system();' \
  -c 'SELECT pg_current_wal_lsn();'
```

Espere a que el candidato aplique ese LSN. Si el primario ya no es accesible,
compare **todas las copias alcanzables**; espere a que la seleccionada aplique
todo el WAL recibido, registre el último LSN conocido y acepte explícitamente
el RPO desconocido. Si existe una copia inaccesible que podría tener WAL más
reciente, documente esa incertidumbre o recupere su acceso antes de proceder.
No declare recuperación sin pérdida por observar solamente `streaming`.

## Aislar primero; promover después

1. El operador debe impedir físicamente que el primario anterior acepte
   escrituras: apagado controlado confirmado o aislamiento de todas sus rutas
   hacia clientes y réplicas. Un ping fallido o detener solamente HAProxy no
   prueba fencing. En Windows/WSL, detener Ubuntu no basta si Docker Desktop
   continúa ejecutando sus contenedores.
2. Si controla Docker en el nodo anterior, detenga el servicio de la base y
   desactive su reinicio automático. Los comandos siguientes son un ejemplo
   local en **el antiguo primario**, para `compras_db`; no los ejecute en el
   candidato. Revise la ruta Compose y la identidad del contenedor antes:

   ```bash
   docker compose stop compras_db
   docker compose ps -a -q compras_db
   docker update --restart=no ID_DEL_CONTENEDOR_CONFIRMADO
   docker compose run --rm --no-deps --entrypoint sh compras_db -c \
     'touch /var/lib/nexustix-pg/NEXUSTIX_FENCED "$PGDATA/NEXUSTIX_FENCED"; sync'
   ```

   Esos marcadores persisten en dos volúmenes y el entrypoint se niega a
   arrancarlos. Detener sin desactivar el reinicio y sin dejar un aislamiento
   persistente no constituye una garantía suficiente. Si la máquina está
   inaccesible, haga y documente fencing externo antes de continuar; el marcador
   se escribirá al recuperar acceso, antes de reconectarla a la LAN.
3. Verifique que no queda otro primario accesible. Anote responsable, momento,
   método/evidencia de aislamiento, identificador del sistema, candidatos y LSN.
   Asegure que sólo un operador está coordinando la operación.
4. En el candidato, use el identificador de sistema y LSN **reales** registrados;
   reemplace los marcadores de ejemplo. El ID del primario aislado es su
   identificador de replicación `nx_compras_<id_nodo_normalizado>`:

   ```bash
   docker compose exec compras_db nexustix-pg-manage promote \
     ID_SISTEMA LSN_MINIMO nx_compras_nodo_anterior \
     --fencing-confirmado-rpo-aceptado
   ```

   La herramienta comprueba identidad, estado de recuperación y WAL aplicado;
   exige la aceptación del fencing y RPO. **No puede comprobar por sí misma el
   apagado de otra PC.** No modifica el host, no detiene otras máquinas, no elige
   otro candidato y no reduce la política síncrona automáticamente. Registra
   `NEXUSTIX_PROMOTED` antes de `pg_promote` para conservar evidencia si
   hay timeout. Ese registro no permite arrancar datos primarios con un
   inventario que aún diga standby: actualice el inventario antes de reiniciar.
   Si falla o vence el plazo, inspeccione el estado de
   ese mismo candidato antes de cualquier decisión adicional.
5. Cambie en el inventario el primario de esa base al nodo recién promovido y
   retire temporalmente al anterior de la asignación activa. Regenerar es un
   paso posterior a promover, no una orden de promoción. Actualice las URLs
   multi-host/secretos y la configuración de todos los nodos afectados según
   la guía principal. Actualice también el inventario del Laboratorio.
6. Prepare los slots en el nuevo primario. Configure los standbys supervivientes
   para seguirlo y confirme recuperación, timeline y streaming. Si PostgreSQL
   rechaza una historia divergente o falta WAL, conserve sus volúmenes y
   reclone en otra generación; no fuerce la recuperación.
7. Restablezca el conjunto síncrono previsto antes de reanudar las escrituras.
   Al promover, la configuración anterior podría esperar al propio candidato
   o a nodos que ya no estén disponibles. Elija explícitamente entre esperar
   réplicas suficientes o aceptar temporalmente un RPO mayor; no degrade esa
   política de manera silenciosa. Compruebe los servicios y sesiones existentes
   antes de retirar el mantenimiento.

Haga el procedimiento por base lógica: Usuarios, Eventos y Compras pueden tener
primarios distintos. Esto no convierte sus transacciones en una sola operación
atómica; los pedidos conservan sus mecanismos de idempotencia y recuperación.

## Reincorporación del antiguo primario

Conserve aislados sus volúmenes de datos, WAL y estado. No retire
`NEXUSTIX_FENCED`, no agregue `standby.signal` a sus datos y no cambie simplemente
su rol en el inventario: un primario antiguo puede contener una historia de WAL
divergente.

Declare ese nodo como standby y aumente su generación para esa base, por
ejemplo:

```yaml
databases:
  compras:
    primary: nodo-nuevo
    standbys: [nodo-anterior, nodo-otro]
    generations:
      nodo-anterior: 2
```

Integre el fragmento en el inventario existente sin omitir sus otros campos.
Regenere, prepare su slot físico en el nuevo primario y arranque la réplica
con los nuevos volúmenes vacíos. `pg_basebackup` crea una copia del primario
vigente. Confirme el identificador del sistema, `recovery=true`, timeline y WAL
antes de considerarla disponible. Sus volúmenes anteriores se conservan con
la generación previa.

## WAL, capacidad, retirada y respaldo

El archivado reutiliza `postgres/archive-wal.sh` y se habilita en ambos roles
con `archive_mode=always`. Cada nodo conserva su propio archivo WAL persistente;
no hay un archivo remoto compartido ni copia fuera de la máquina. El archivado
local no protege de perder todos los discos de ese nodo. No se ha preparado
una restauración PITR automática: una recuperación PITR requiere una copia
base compatible, su secuencia WAL completa y un procedimiento aislado.

Los slots retienen como máximo 2048 MB de WAL por configuración del servidor,
con el margen propio de los checkpoints. Un standby demasiado atrasado puede
perder su slot utilizable y necesitar una clonación nueva. Los archivos WAL
archivados no tienen limpieza automática: vigile espacio y defina retención
según respaldos reales antes de su uso prolongado. Replicación no sustituye
respaldos: errores lógicos y borrados también se replican.

Para retirar una réplica, quítela del conjunto síncrono y del inventario,
regenere y aplique la configuración. Detenga después su servicio y conserve
sus volúmenes. El slot no se elimina automáticamente. En el primario, tras
comprobar que corresponde al nodo retirado y que está inactivo, el operador
puede eliminar exclusivamente ese slot:

```sql
SELECT slot_name, active, restart_lsn, wal_status FROM pg_replication_slots;
-- Sustituir por el nombre exacto comprobado; no retirar slots aún necesarios.
SELECT pg_drop_replication_slot('nx_compras_nodo_retirado');
```

Cambiar archivos de contraseña no rota automáticamente contraseñas de roles
en una base existente. La rotación necesita coordinación del administrador,
aplicaciones y réplicas; no está implícita en regenerar el inventario.

Los scripts antiguos `scripts/db_ha.py` siguen perteneciendo a sus propios
entornos y topologías. Esta implementación reutiliza sus principios de WAL,
multi-host y fencing, no sus comandos de dos copias ni sus nombres de volumen.
No los ejecute contra los contenedores multinodo.

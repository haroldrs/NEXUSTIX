# Limitaciones y alcance verificado — 3 de noviembre de 2026

Esta revisión sustituye el diagnóstico anterior de Windows. El desarrollo y
las pruebas actuales se ejecutaron en Fedora, manteniendo separado el nuevo
laboratorio `nexustix-distributed-lab` del staging existente. La evidencia y
los resultados por escenario están en [RESULTADOS_DISTRIBUIDO.md](RESULTADOS_DISTRIBUIDO.md).

## Implementado y validado

- Registro/login, autorización, gestión de eventos, pedidos individuales y
  grupales, pago simulado, entradas QR, acceso único, rechazo y reembolso.
- Exclusión de inventario y asientos en PostgreSQL, claves idempotentes,
  recuperación durable de Saga y outbox con notificaciones deduplicadas.
- Dos réplicas de Compras y continuidad HTTP por Nginx al detener una réplica
  del laboratorio; recuperación de pedidos tras reiniciar ambas.
- Tres primarios y tres standbys Docker PostgreSQL 15.19 en streaming.
- Promoción, reconexión del pool y reintegración a un directorio nuevo en
  clústeres PostgreSQL 18.6 locales efímeros. El primario anterior se detuvo
  realmente; el adaptador de fencing Docker fue sustituido en esa prueba.
- Backup físico verificado y PITR real mediante WAL a un punto nombrado,
  además del respaldo/restauración lógico probado previamente.

## Implementado con validación pendiente o alcance parcial

| Tema | Límite actual |
|---|---|
| Despliegue VM | Manifiestos y procedimientos preparados y validados con `compose config`; ninguna VM arrancada ni red configurada. Comunicación, fencing remoto y fallos de VM PENDIENTES. |
| Failover PostgreSQL | Manual, con aislamiento previo del primario. No hay elección automática ni consenso Patroni/etcd. La orquestación Docker heredada de `db_ha.py` no se ejecutó contra staging ni contra las futuras VMs. |
| Alta disponibilidad de aplicación | Usuarios, Eventos, frontend y balanceador tienen una instancia. Dos Compras en una sola VM toleran una caída de proceso, pero no la caída de esa VM. |
| Balanceador | Nginx OSS usa detección pasiva y reenvío; no hay health checks activos del upstream ni balanceador redundante. |
| Backup programado | Script diario y plantillas systemd preparados; timers sin instalar ni ejecutar. Retención genera candidatos para revisión, nunca elimina. Copia externa automática pendiente. |
| Interfaz | Sintaxis JS y estado de autenticación Node comprobados; los flujos se probaron por HTTP. Validación interactiva completa de todos los formularios en navegador pendiente. |
| Compatibilidad | Se mantienen `/compras` y `/compras/grupal`. La idempotencia grupal y el checkout de pago/QR corresponden a `/ordenes`, activado con `CHECKOUT_ORDERS`. No se migró el staging en ejecución. |
| Catálogo | Fecha, lugar, zonas, asientos y precios al crear; edición posterior de metadatos y activación. Reconfigurar asientos/precios vendidos no está implementado. Eventos históricos conservan precio cero hasta una gestión explícita. |
| Recuperación de pedidos | El worker revisa hasta 1000 pedidos activos por barrido. Volúmenes mayores requieren paginación/priorización para evitar postergar pedidos. Durante una caída la liberación por vencimiento puede retrasarse. |
| Notificaciones | Buzón persistente interno; sin correo, SMS ni broker externo. |
| Observabilidad | IDs de correlación/instancia, logs JSON y consultas reales de inventario, pedidos, rol PostgreSQL, lag y archivador. Sin Prometheus/alertas centralizadas. Métricas legacy en memoria se pierden al reiniciar. |
| Rendimiento | Pruebas de concurrencia focalizadas, sin benchmark de carga ni garantías de capacidad o latencia. |

## Consistencia y recuperación

Inventario, pagos simulados y consumo de QR usan transacciones/bloqueos en su
base de autoridad. Saga y outbox coordinan efectos entre servicios, con
ventanas de estado intermedio: **no existe ACID distribuido**. No se promete
confirmación global instantánea durante una interrupción.

La replicación por defecto es **asíncrona**: un failover puede perder WAL aún
no replicado. Las tres bases se replican independientemente; promoverlas exige
auditar pedidos e inventario y reconciliar diferencias antes de reabrir venta.
La opción síncrona fue ejercitada con el standby presente; su ausencia puede
bloquear commits. No se cambia automáticamente a asíncrono.

Promover sin aislar al primario anterior puede producir split-brain. Las URLs
multi-host con `target_session_attrs=read-write` eligen un nodo escribible, pero
no sustituyen al fencing. Un timeout o ping fallido no acredita aislamiento.

Backups y WAL en volúmenes del mismo host no protegen de la pérdida del host.
Un backup de una base no constituye un punto consistente global de las tres.
La guía exige detener escritores y coordinar puntos de recuperación cuando
se requiera restauración conjunta. No se declara un RPO/RTO garantizado.

## Seguridad y operación

- Entorno de demostración con HTTP, sin TLS ni rate limiting de login/registro.
  Acceso local publicado en loopback; antes de exposición externa hacen falta
  TLS, reglas de acceso, copias externas y revisión operativa.
- Sin verificación de correo ni recuperación de contraseña. Los cambios de rol
  revocan refresh tokens; JWT de acceso existentes conservan vigencia hasta su
  expiración. QR es una credencial portadora: quien lo posee puede presentarlo,
  pero solo una validación autorizada puede consumirlo una vez.
- Contenedores API ejecutan como root para leer los secretos 0600; endurecimiento
  con UID dedicado pendiente. Secretos fuera del repositorio, sin publicar en
  informes. Pagos simulados: nunca se procesan tarjetas reales.
- El reloj Lamport y la elección Bully heredados son didácticos; no eligen al
  primario PostgreSQL ni coordinan el nuevo inventario.
- `unless-stopped` no reanuda contenedores detenidos manualmente. Construir por
  primera vez necesita las imágenes y dependencias disponibles.
- Todas las VMs sobre el mismo equipo compartirían su fallo físico. Para HA de
  aplicación hace falta otra VM y entrada redundante; un cuarto monitor por sí
  solo no elimina ese límite.

No se ejecutaron las suites históricas que detienen/reinician staging. Los
ensayos destructivos se limitaron a procesos efímeros nuevos o a detener y
reanudar Compras en el laboratorio. No se eliminaron ni reinicializaron los
volúmenes existentes.

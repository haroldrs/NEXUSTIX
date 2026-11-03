# Actualización de persistencia — 3 de noviembre de 2026

Usuarios avanza a revisión `20261102_02`; Compras a `20261102_03`. Migraciones
aditivas ensayadas en bases aisladas. Compras añade `ordenes`, `pagos_simulados`,
`entradas`, `outbox` y `notificaciones`. Eventos añade metadatos, precio,
`asientos`, `operaciones_cerradas` y `auditoria_catalogo`; DDL serializado al
arrancar. Ninguna migración se aplicó al staging existente.

El laboratorio nuevo tiene tres standbys de streaming y WAL archivado.
Backup físico, PITR y política de retención:
[RECUPERACION](../despliegue-vm/RECUPERACION.md). El respaldo lógico existente
ahora pausa también Eventos, que admite escrituras administrativas. Los
resultados reales están en [RESULTADOS_DISTRIBUIDO](RESULTADOS_DISTRIBUIDO.md).

La sección siguiente documenta el entorno legacy; sus nombres de volúmenes
no deben utilizarse para el laboratorio nuevo.

---

# Bases de datos: persistencia, respaldo, restauración y replicación

## 1. Persistencia

| Base | Volumen demo | Volumen pruebas | Contenido |
|---|---|---|---|
| `usuarios_db` | `nexustix_usuarios_data` | `nexustix_auth_staging_usuarios_data` | usuarios, refresh_sessions, alembic_version |
| `eventos_db` | `nexustix_eventos_data` | `nexustix_auth_staging_eventos_data` | eventos (stock, stock_inicial), reservas_stock |
| `compras_db` | `nexustix_compras_data` | `nexustix_auth_staging_compras_data` | compras, purchase_idempotency, journal_compras, alembic_version |

- Los nombres son explícitos: no dependen de la carpeta donde se descomprime
  el proyecto. Para reutilizar volúmenes de la versión anterior (que se
  llamaban `<carpeta>_usuarios_data`) defina `NEXUSTIX_VOLUME_PREFIX`.
- `docker compose stop/start/restart` y `down` **sin** `-v` conservan los
  volúmenes. Ningún script del proyecto borra volúmenes.
- `POSTGRES_PASSWORD_FILE` solo actúa al inicializar un volumen vacío; un
  volumen existente nunca se reinicializa.
- Healthcheck de cada nodo: `pg_isready` por TCP (no cuenta el servidor
  temporal de `initdb`) + `SELECT 1`.
- Servidor con `tcp_keepalives_*` (detecta clientes muertos en ~25 s) e
  `idle_in_transaction_session_timeout=120s`: una réplica de Compras eliminada
  sin cerrar su conexión no retiene bloqueos de fila indefinidamente.
- Las APIs usan `pool_pre_ping` y keepalives de libpq: tras reiniciar o
  promover PostgreSQL descartan conexiones muertas y se reconectan solas.

## 2. Esquema y migraciones

- **Usuarios** (`20261022_01`) y **Compras** (`20261023_02`) usan Alembic.
  Cada contenedor ejecuta `python -m shared.run_migrations` antes de Uvicorn,
  con un *advisory lock* (compras1 y compras2 pueden arrancar a la vez):

  | Estado de la base | Acción |
  |---|---|
  | Vacía (instalación nueva) | crea el esquema |
  | Revisión conocida anterior (actualización) | `upgrade head` (migraciones aditivas) |
  | Ya en la revisión esperada | nada |
  | Revisión desconocida | se niega; la API no arranca |
  | Tablas sin `alembic_version` (legacy) | se niega salvo `NEXUSTIX_ALLOW_LEGACY_BASELINE=1` tras respaldar |

  `NEXUSTIX_AUTO_MIGRATE=0` deja las migraciones totalmente manuales. Los
  `downgrade` se detienen si borrarían cuentas, sesiones o compras.
- **Eventos** no usa Alembic: al arrancar ejecuta DDL idempotente y aditivo
  (restricciones `stock >= 0`, columna `stock_inicial` rellenada solo para los
  cinco eventos base, nunca sobrescribe un valor existente).

## 3. Respaldo

```bash
python3 scripts/nx.py demo respaldo --destino ~/nexustix-backups/demo-$(date +%Y%m%d-%H%M)
```

1. Pausa escrituras (`stop usuarios compras1 compras2`; Eventos solo escribe a
   pedido de Compras) y las reanuda al terminar, también si falla.
2. Localiza cada base **por etiquetas Compose del proyecto exacto** y, con
   replicación, en el nodo que acepta escrituras (dos primarios ⇒ aborta).
3. Rechaza el respaldo si hay reservas `reservada` (reconciliar antes).
4. `pg_dump --format=custom` por base, SHA-256 en `manifest.json` y
   `SHA256SUMS`, modo 0600, directorio nuevo (nunca sobrescribe).
5. Registra agregados de negocio, filas por tabla y huella de esquema
   (tablas, columnas, restricciones, FKs, índices, revisión Alembic) antes y
   después; si cambian durante el volcado, el respaldo falla.
6. Verifica en aislamiento (ver 4).

**Qué garantiza:** tres volcados lógicos consistentes entre sí *mientras las
escrituras están pausadas*, verificables por hash y restaurables.
**Qué deja fuera:** no es un snapshot atómico de las tres bases sin pausa; no
incluye WAL ni permite recuperación a un instante (PITR); no incluye secretos
(guárdelos aparte), imágenes ni configuración del host; `refresh_sessions` es
volátil (cambia con cada login y se excluye de la comparación estricta).
Guarde una copia fuera de la PC (otro disco o equipo).

## 4. Verificación y restauración

```bash
# Verificación aislada (red interna + PostgreSQL en tmpfs; no toca ningún entorno)
python3 scripts/backup_main_databases.py verify --backup-dir DIR [--rehearse-migrations]
```

Restaura los tres volcados en un contenedor temporal, compara agregados,
filas y esquema, y opcionalmente ensaya las migraciones sobre la copia. Deja
`verification.json`; si falla conserva los recursos temporales para diagnóstico.

**Procedimiento ante pérdida de datos** (restaurar en bases vacías):

1. Detenga el entorno afectado: `python3 scripts/nx.py demo stop`.
2. Conserve los volúmenes dañados para análisis (no los borre); cree una
   instalación con volúmenes nuevos, por ejemplo con otro prefijo:
   `export NEXUSTIX_VOLUME_PREFIX=nexustix-restaurado`.
3. Arranque solo las bases: `docker compose -p nexustix -f docker-compose.yml up -d --wait usuarios_db eventos_db compras_db`
   (con `NEXUSTIX_SECRETS_DIR` exportado).
4. Restaure (se niega si alguna base tiene tablas o el respaldo no está verificado):
   `python3 scripts/backup_main_databases.py restore --entorno demo --compose-project nexustix --backup-dir DIR --confirm-empty-target`
5. `python3 scripts/nx.py demo up` y `python3 scripts/nx.py demo verificar`;
   revise la auditoría desde el panel de administración.

Nunca restaure sobre las bases originales para "probar" un respaldo: para eso
existe `verify`.

## 5. Replicación y alta disponibilidad

### Decisión

Se implementó **replicación física en streaming primario → standby en
caliente** para las tres bases (overlay `docker-compose.replicacion.yml`) con
**promoción manual controlada** y fencing. **No** se implementó failover
automático: con dos nodos no hay quórum, y un árbitro automático que viera
"primario caído" durante una partición de red podría crear dos primarios.

### Cómo funciona

- Cada `<base>_replica` se clona con `pg_basebackup -R` desde su primario al
  arrancar con el volumen vacío y sigue en modo standby (lecturas permitidas,
  escrituras rechazadas). Un slot físico por standby evita perder WAL
  (limitado a 1 GiB con `max_slot_wal_keep_size`).
- Las APIs usan `…?host=<base>:5432&host=<base>_replica:5432&target_session_attrs=read-write`:
  libpq prueba los nodos en orden y solo acepta el que admite escrituras.
- `db_ha.py promote <base>`: comprueba que hay exactamente un standby, **aísla**
  al primario (lo detiene, desactiva su reinicio y escribe `NEXUSTIX_FENCED`
  en su volumen; el entrypoint se niega a arrancarlo con código 64), promueve
  el standby (`pg_promote`) y espera a que acepte escrituras. Las conexiones
  nuevas de las APIs llegan solas al nuevo primario.
- `db_ha.py rejoin <base> <nodo>`: archiva el volumen del nodo aislado
  (`~/nexustix-archivo-nodos/*.tar.gz`), lo vacía y lo vuelve a clonar como
  standby del primario actual. Nada se pierde sin copia.
- `db_ha.py switchback <base>`: conmutación planificada de vuelta (espera a
  que el standby aplique todo el WAL, promueve el nodo original y reincorpora
  la réplica).
- `db_ha.py status` informa rol, LSN, retraso y slots, y marca `DOS_PRIMARIOS`
  si lo detectara; ningún script actúa automáticamente en ese caso.

```bash
python3 scripts/db_ha.py pruebas enable        # primarios con parámetros de replicación + standbys
python3 scripts/db_ha.py pruebas status
python3 scripts/db_ha.py pruebas promote compras_db
python3 scripts/db_ha.py pruebas rejoin compras_db compras_db
python3 scripts/db_ha.py pruebas switchback compras_db
python3 scripts/db_ha.py pruebas sync compras_db on    # síncrona (RPO 0) / off
python3 scripts/db_ha.py pruebas disable       # volver a un nodo por base (conserva volúmenes)
```

### Síncrona vs asíncrona

| Modo | Pérdida posible (RPO) | Riesgo |
|---|---|---|
| Asíncrona (por defecto) | Las transacciones confirmadas en el primario y aún no recibidas por el standby (normalmente < 1 s en la misma PC) | Tras promover, la auditoría puede mostrar reservas confirmadas sin compra o compras sin reserva si las bases quedaron en instantes distintos: ejecutar `/compras/recuperar-servidor` y revisar. |
| Síncrona (`sync on`, `ANY 1 (*)`) | 0 para transacciones confirmadas | Si el standby cae, el primario **espera** en cada COMMIT: las compras se bloquean hasta desactivarla (`sync off`). |

Tiempo de recuperación (RTO) con promoción manual: lo que tarde el operador
en decidir + el tiempo de promoción y reconexión. Medido sin Docker sobre
PostgreSQL 15.19 real (`tests/postgres/test_pg_replicacion.py`): **4,6 s**
desde `promote` hasta la primera compra correcta en la misma instancia de
Compras. Con Docker lo mide `test_r21` (pendiente).

### Lo que falta para alta disponibilidad real

1. Un tercer miembro con voto (quórum): Patroni + etcd/Consul con tres nodos,
   o `pg_auto_failover` con un *monitor* en un tercer equipo.
2. Fencing fiable entre máquinas (apagar el nodo antiguo por IPMI/hipervisor o
   aislarlo de la red) antes de promover.
3. Nodos en hosts distintos y red entre ellos con latencia baja (síncrona).
4. Un punto de entrada estable (VIP/HAProxy) si hay clientes fuera de Docker.

Hasta entonces el sistema ofrece **redundancia de datos y conmutación
controlada**, no alta disponibilidad automática.

## 6. Consistencia entre bases

Compras y Eventos se coordinan por saga (ver `ARQUITECTURA.md` §3). Tras
cualquier incidente:

1. `GET /auditoria/consistencia` (panel Observabilidad, administrador).
2. `POST /compras/recuperar-servidor`: confirma reservas de compras ya
   persistidas, compensa reservas huérfanas de claves abandonadas y repara
   respuestas idempotentes; es repetible y no actúa sin evidencia de ambas bases.
3. `POST /eventos/reconciliar-stock` (preview por defecto) solo si la auditoría
   lo indica y no hay reservas pendientes.

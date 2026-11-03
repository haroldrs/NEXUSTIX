# Auditoría técnica de tolerancia a fallos NEXUSTIX

**Corte:** 2026-10-24. Incluye recuperación, pruebas unitarias aisladas y validaciones HTTP/PostgreSQL en el Compose aislado de staging, paradas ordenadas individuales de ambas réplicas y fallos abruptos controlados de Compras1 en cuatro checkpoints. El intento equivalente para Compras2 no pasó la aserción de estado detenido y queda pendiente. No se consultaron ni modificaron las bases principales, no se ejecutaron migraciones ni se detuvieron servicios del entorno principal.

## Resumen ejecutivo

NEXUSTIX tiene dos procesos Compras que comparten `compras_db`, un Nginx con round robin pasivo y bases separadas para Usuarios, Eventos y Compras. Las compras individuales autenticadas usan `Idempotency-Key`, persisten la respuesta y coordinan stock mediante reservas idempotentes en Eventos. La migración `20261023_02` agrega esa tabla y no se modificó ni aplicó durante esta auditoría.

La duplicación de Compras solo protege el proceso Compras. Usuarios, Eventos, las tres bases, Nginx y el host siguen siendo puntos únicos de fallo. La saga Compras–Eventos no es una transacción ACID distribuida. La idempotencia conserva la clave y `operacion_id` antes de llamar a Eventos; el lock PostgreSQL evita ejecutar en paralelo un mismo key. Esta iteración incorpora conciliación repetible por `operacion_id`, consulta exacta del estado durable de reserva y actualización del replay con evidencia de Eventos. En staging se validaron recuperación por HTTP, paradas ordenadas individuales de `compras1`/`compras2` y salidas abruptas de Compras1 mediante `os._exit(86)` en cuatro checkpoints. No se probó caída abrupta de Compras2 ni la indisponibilidad de Eventos o PostgreSQL.

## 1. Arquitectura actual

```text
Navegador
  ├─ Frontend Nginx :8080 (archivos estáticos)
  ├─ Usuarios :8001 ───────── usuarios_db PostgreSQL 15 + volumen
  │    └─ Firma JWT RS256; refresh sessions revocables
  ├─ Eventos :8002 ────────── eventos_db PostgreSQL 15 + volumen
  │    └─ Stock/reservas; autentica llamadas internas de Compras
  └─ Compras Nginx :8005 (round robin pasivo, 2 intentos)
       ├─ Compras1 :8003 ─┐
       └─ Compras2 :8004 ─┴─ compras_db PostgreSQL 15 + volumen
                  └─ invoca Eventos para reservar/confirmar/liberar

Todo reside en un host/Compose. Las tres bases son independientes y no tienen
réplicas PostgreSQL declaradas.
```

| Componente | Responsabilidad/dependencias | Endpoints principales | Disponibilidad |
|---|---|---|---|
| Frontend Nginx | Sirve UI estática | `:8080` | Una instancia. |
| Usuarios | Registro, login, refresh/logout, usuarios y roles; requiere `usuarios_db` y clave privada | `POST /auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`; `GET /auth/me`, `/health` | Una instancia. |
| Eventos | Catálogo, stock y reservas; requiere `eventos_db` y token interno | `GET /eventos`, `GET /eventos/{id}/stock`, `POST /eventos/{id}/reservar`, `/confirmar`, `/liberar`, `GET /eventos/reservas/pendientes`, `/eventos/auditoria/stock`, `/health` | Una instancia y una base. |
| Compras1/2 | Verifican JWT localmente, coordinan clave en DB compartida y llaman Eventos | `POST /compras`, `/compras/grupal`, `/compras/recuperar-servidor`; `GET /compras/mias`, `/compras`, `/compras/journal`, `/auditoria/consistencia`, `/health` | Dos instancias; dependencia de Nginx, DB Compras y Eventos. `/compras/grupal` no usa `Idempotency-Key`. |
| Nginx Compras | Round robin y detección pasiva | `:8005` | Una instancia, sin checks activos. |
| PostgreSQL | Persistencia por servicio | No se publican puertos en Compose | Cada DB tiene un contenedor/volumen en el mismo host. |

### Conexiones y configuración

- Compose principal publica `8080`, `8001–8005`; sus bindings no limitan a
  loopback. Staging publica `127.0.0.1:18080–18085` y utiliza nombres,
  volúmenes y secretos propios.
- Compras1/2 comparten URL de `compras_db`, clave pública JWT y token interno
  para Eventos. El código de compra valida JWT localmente; `USUARIOS_URL` está
  configurado pero el flujo inspeccionado no llama a Usuarios para validar cada
  compra.
- Compose principal tiene healthchecks para PostgreSQL, pero no healthchecks de
  aplicación. Compras depende de Usuarios/Eventos con `service_started`, no
  readiness. Nginx tampoco consulta `/health`.
- Los health endpoints pueden responder HTTP 200 con `status=degraded` cuando
  falla la DB. El balanceador no retira ese proceso por ese estado.
- El Compose principal declara credenciales DB directamente en su configuración.
  No se repiten aquí sus valores. Staging monta secretos desde archivos.
- `compras_lb/nginx.conf` establece `max_fails=2`, `fail_timeout=10s`, timeout
  de conexión de 2 s y hasta dos intentos ante errores/timeouts/502/503/504.
  No activa `non_idempotent`; Nginx no reenvía por defecto un POST ya enviado.
  Esto evita repetir endpoints sin idempotencia, pero un cliente debe reintentar
  compra con el mismo key.

## 2. Persistencia y consistencia

- Usuarios: migraciones `20261022_00` (baseline no destructivo) y `20261022_01`
  (hash, roles, estado de autenticación y sesiones refresh). Usuarios antiguos
  quedan `legacy_unclaimed`, sin atribución automática de identidad.
- Compras: `20261022_00` (baseline), `20261022_01` (`auth_subject` nullable) y
  `20261023_02` (`purchase_idempotency`: key única, sujeto, hash, `operacion_id`
  único, estado y respuesta). Las compras históricas se conservan.
- Eventos no tiene Alembic en los archivos inspeccionados: en startup usa
  `Base.metadata.create_all`, agrega una constraint si falta y crea seed si la
  tabla está vacía. El esquema queda sin versionamiento explícito.
- Eventos persiste stock y `ReservaStock` en una transacción, con check de stock
  no negativo, cantidad positiva, `operacion_id` como PK y lock de la fila del
  evento. Los estados son `reservada`, `confirmada`, `liberada`.
- Compras guarda compra y respuesta idempotente en una transacción local; luego
  confirma la reserva en Eventos. Los commits de ambas bases no son atómicos.
- `journal_compras` se escribe aparte del commit de compra y su fallo no cancela
  la venta. Es bitácora funcional best-effort, no WAL ni fuente de verdad.
- `/auditoria/consistencia` compara stock/ventas, correlación de operaciones,
  reservas pendientes y compras huérfanas. Reporta brechas del journal aparte.
  La reconciliación de stock en Eventos usa `EVENTOS_BASE` y reservas
  confirmadas, no consulta `compras_db`; no aplicar cambios sin conciliación
  previa de ambos servicios.

## 3. Matriz de escenarios

Clasificaciones: **Implementado y probado**, **Implementado pero no probado**,
**Parcialmente implementado**, **No implementado**, **No verificable**. Las
pruebas en memoria/SQLite no cuentan como validación de PostgreSQL.

| # | Escenario | Clasificación y evidencia | Recuperación/límite |
|---:|---|---|---|
| 1 | Se detiene Compras1; Compras2 sigue | **Validado en staging con parada ordenada y salida abrupta controlada en checkpoints de compra.** La otra réplica siguió atendiendo; reintentos no duplicaron. | La salida abrupta se probó solo sobre Compras1 y con hook de test explícito; no prueba pérdida del host ni Nginx/Eventos/DB. |
| 2 | Se detiene Compras2; Compras1 sigue | **Validado en staging con parada ordenada de contenedor.** La compra pasó por el balanceador mientras Compras1 estaba activa. | El intento de salida abrupta de Compras2 quedó fallido/inconcluso; Nginx, DB y Eventos siguen únicos. |
| 3 | Instancia cae después de reclamar clave | **Salida abrupta de Compras1 probada en staging después del commit del claim.** El claim quedó `processing`, sin compra/reserva; retry completó una compra y conservó `operacion_id`. | Recovery por endpoint no se invocó: el reintento con la misma clave fue suficiente. No hay worker periódico para claims abandonados sin retry. |
| 4 | Cae después de reservar y antes de compra | **Salida abrupta de Compras1 probada en staging.** El claim `processing` y la reserva `reservada` persistieron, sin compra; retry reutilizó el `operacion_id`, confirmó reserva y creó una compra. | Stock no se debitó por segunda vez. No se probó salida abrupta de Compras2 ni una indisponibilidad simultánea de Eventos. |
| 5 | Eventos timeout, 5xx o ambiguo | **Implementado; timeout probado en unidad.** Timeout/5xx deja clave `processing`, sin liberar a ciegas. | Retry usa mismo ID. No se ejecutó HTTP real ante fallo de Eventos. |
| 6 | Cliente reintenta resultado desconocido | **Parcialmente implementado y probado parcialmente.** Backend reproduce respuesta al repetir key/usuario/hash. | Frontend guarda clave pendiente en memoria; reload la pierde. Sin key original, consultar historial antes de iniciar otra compra. |
| 7 | Ambas réplicas reciben mismo key a la vez | **Implementado; prueba HTTP reportada por el usuario.** Key única y row lock en DB compartida. | Usuario reporta test staging aprobado; no se reprodujo durante esta auditoría. |
| 8 | Compras concurrentes sobre-venden | **Parcialmente implementado.** Eventos bloquea fila de stock y comprueba cantidad; existe constraint no negativo. | La prueba staging reportada intenta cantidad superior al stock, no prueba necesariamente dos keys distintos en una carrera por el último stock. |
| 9 | `compras_db` deja de responder | **Parcialmente implementado.** Falla la compra; el flujo intenta compensar y no libera si no puede verificar commit. | DB compartida es SPOF; no hay replicación/conmutación. |
| 10 | `eventos_db` deja de responder | **Parcialmente implementado.** Eventos es llamada síncrona; timeout/5xx mantiene resultado ambiguo. | Servicio/DB únicos; no se puede verificar ni recuperar hasta volver a responder. |
| 11 | Se pierde red Compras–Eventos | **Parcialmente implementado.** Timeouts HTTP de 5 s y clave durable. | No hay cola durable; retry cliente o recuperación manual. |
| 12 | Cae Nginx | **No implementado.** Un balanceador. | Compra por URL del frontend falla hasta que Nginx vuelva. |
| 13 | Reinicio con operaciones pendientes | **Parcialmente implementado.** DB/volúmenes conservan datos; arranque verifica revisión Alembic; existe recovery admin. | No hay recovery worker de startup. Métricas, Bully y reloj Lamport locales se reinician. |
| 14 | Diferencia compra/reserva/stock/journal | **Parcialmente implementado; brechas de journal y estados de recovery probados en unidad.** Auditoría es de lectura; recovery/reconciliación pueden escribir. | Journal best-effort; la nueva prueba staging coteja un evento sintético, una reserva, una compra, stock y un COMMIT journal. No compara todavía toda la reconciliación entre bases. |
| 15 | Restaurar desde backup | **Procedimiento implementado; resultado previo reportado como verified.** Script restaura copia en red interna/`tmpfs`, verifica hash/estadísticas y puede ensayar migraciones. | No releí ni verifiqué el respaldo en esta auditoría; prueba Docker de restore está opt-in. |
| 16 | Pérdida de host | **No implementado.** Servicios y volúmenes residen en un host. | Hace falta respaldo off-host y recuperación en host diferente. No hay HA del host. |
| 17 | Carga satura servicios | **No verificable/parcial.** Métricas son locales; no encontré prueba de carga ni ajuste explícito de workers/pool/límites. | Requiere baseline y ensayo en staging; no autoscaling. |

### Recuperación implementada en esta iteración

- `GET /eventos/reservas/{operacion_id}` está protegido con la credencial
  interna y devuelve el estado durable, evento y cantidad exactos. Es una
  consulta de solo lectura.
- `/compras/recuperar-servidor` considera reservas pendientes, claims
  `processing` antiguos y respuestas completadas que aún dicen
  `confirmacion_reserva=pendiente`. La antigüedad solo selecciona candidatos;
  no autoriza por sí misma a confirmar o liberar stock.
- En Compras, `SELECT ... FOR UPDATE NOWAIT` evita recuperar un claim cuyo lock
  sigue retenido por una solicitud activa. Se revisa de nuevo el estado tras
  obtener el lock.
- Si existe compra y Eventos dice `confirmada`, recovery actualiza atómicamente
  la respuesta idempotente para replay, conservando `id` y `operacion_id`. Si
  existe compra y la reserva sigue `reservada`, solicita confirmación idempotente
  y solo entonces actualiza el replay.
- Una reserva sin compra se libera únicamente si su claim sigue `processing`,
  es antiguo y Eventos confirmó el estado exacto `reservada`. Una solicitud
  ambigua, un claim activo, reserva ausente, reserva confirmada sin compra o
  discrepancia de evento/cantidad produce diagnóstico y no cambia esas filas.
- Claim `processing` sin reserva queda diagnosticado y sin cambios para que un
  retry de la misma clave pueda continuar con el mismo `operacion_id`. No se
  añadió migración: `created_at`, estado, respuesta, clave y `operacion_id`
  existentes bastan para esta política. No hay heartbeat ni worker: la ruta
  requiere invocación administrativa.
- La compra solo informa/cachea confirmación cuando Eventos devuelve
  `estado_reserva=confirmada`. Si falla el commit del replay después de esa
  confirmación, devuelve 503 con el mismo `operacion_id`; retry/recovery puede
  corregir la respuesta durable.

### Estados ambiguos identificados

- El estado durable sigue teniendo solo `processing` y `completed`, no etapas
  `reserved`, `purchase_persisted`, `event_confirmed` o `compensated`.
- Recovery no es worker ni se dispara al reiniciar. Los claims sin reserva
  permanecen `processing` hasta retry con la misma clave o revisión del
  diagnóstico por administrador.
- El umbral usa `created_at`, no heartbeat. La protección de una solicitud
  activa depende de que su transacción mantenga el lock PostgreSQL; debe
  validarse con la versión desplegada y latencias reales.
- La transacción y el row lock abarcan una llamada de red de hasta 5 s, lo que
  puede elevar duración de transacción y conexiones bajo carga.

## 4. Riesgos prioritarios

1. **SPOF de servicios/datos:** Usuarios, Eventos, cada PostgreSQL, Nginx y host.
2. **POST ambiguo:** Mantener `non_idempotent` desactivado es prudente mientras
   `/compras/grupal` no tenga idempotencia; el cliente debe repetir el mismo key.
3. **Recovery aún no validada en PostgreSQL real:** el código y las regresiones
   unitarias están; falta probar fronteras de caída y locks de recovery contra
   staging.
4. **Reconciliación de stock:** el baseline es `EVENTOS_BASE`; una aplicación
   automática sin contrastar compras puede corregir hacia una referencia
   incorrecta.
5. **Scripts de prueba no aislados/antiguos:** `scripts/prueba_punto3_tolerancia.py`
   usa Compose implícito y detiene/arranca servicios; sus payloads anteceden a
   autenticación. `scripts/prueba_punto1_consistencia.py` no envía credencial
   interna ni JWT. No se ejecutaron. `scripts/reset_entorno_pruebas.sh` es
   destructivo. Adaptarlos antes de cualquier uso.
6. **Credenciales en Compose principal:** hay credenciales de DB definidas en
   configuración, sin reproducir sus valores aquí.
7. **Eventos sin esquema versionado:** DDL y seed se ejecutan en startup.
8. **Health/observabilidad:** métricas por proceso, health HTTP 200 puede indicar
   degradación y Nginx no evalúa readiness.
9. **Token interno estático:** autenticación entre servicios presente; no se
   encontró protocolo de rotación gradual.

## 5. Pruebas existentes y resultados

### Ejecutadas en esta iteración

```sh
/tmp/nexustix-idempotency-test-venv/bin/python -m pytest -q \
  tests/test_authentication.py tests/test_backup_procedure.py \
  tests/test_backup_restore_isolated.py
```

Comando combinado ejecutado tras los cambios: **25 passed, 1 skipped** en 4.31 s
para `tests/test_authentication.py`, `tests/test_backup_procedure.py` y
`tests/test_backup_restore_isolated.py`. Incluye regresiones de recovery con
SQLite temporal y dobles HTTP para replay confirmado, claim sin reserva, lock
activo, error de DB distinto de lock, timeout consultando Eventos y repetición
segura. El skip requiere Docker/backup aprobado. Esto no prueba locks ni
concurrencia reales de PostgreSQL.

En una ejecución anterior se reportó **19 passed, 1 skipped** en pruebas de
autenticación y backup; esa cifra no es el resultado de esta iteración.

```sh
/tmp/nexustix-idempotency-test-venv/bin/python -m pytest -q tests/staging/test_auth_http.py
```

Antes de iniciar staging, la invocación sin `NEXUSTIX_STAGING_HTTP` produjo
**21 skipped** en 0.43 s; esas omisiones no se contaron como aprobadas.

### Validación integrada en staging — 2026-10-23

Se confirmó el proyecto Compose `nexustix-auth-staging`, el archivo
`docker-compose.staging.yml`, las rutas de secretos existentes sin leer su
contenido, los puertos loopback `18080–18085` y las tres bases PostgreSQL
saludables. Compose pasó `config --quiet`; Compras estaba en Alembic
`20261023_02`. Los tres contenedores de base mantuvieron sus IDs antes/después.
No se iniciaron, detuvieron ni modificaron recursos del proyecto principal.

Se construyeron solo las imágenes `nexustix-staging/eventos:auth` y
`nexustix-staging/compras:auth`. Como Compras1 y Compras2 usan la misma imagen,
se actualizó una réplica por vez con `up -d --no-deps`: Eventos, Compras1 y
Compras2. Usuarios, frontend, balanceador y bases no se reconstruyeron ni
recrearon.

Comprobaciones HTTP después de cada actualización: frontend, Usuarios, Eventos,
Compras1, Compras2 y balanceador respondieron `200`; las APIs reportaron
`db=connected`. En el endpoint nuevo de Eventos, una consulta anónima respondió
`401`; la consulta con credencial interna a un `operacion_id` deliberadamente
inexistente respondió `404`, lo que confirma autenticación aceptada y ausencia
del registro consultado.

Pruebas HTTP ejecutadas individualmente con `NEXUSTIX_STAGING_HTTP=1` y la ruta
de secretos de staging:

| Prueba | Resultado |
|---|---:|
| `test_concurrent_different_buyers_cannot_oversell_synthetic_staging_event` | **1 passed, 3.29 s** |
| `test_shared_postgres_idempotency_across_both_purchase_replicas` | **1 passed, 1.01 s** |
| `test_purchase_idempotency_replays_and_rejects_changed_request` | **1 passed, 1.18 s** |
| `test_concurrent_purchase_retries_create_one_order` | **1 passed, 0.73 s** |

La primera ejecución de la prueba sintética falló en el parser del test: `psql`
devolvió también el tag `INSERT 0 1`. No llegó a enviar compras. Se consultó el
ID generado, se comprobó stock 1, cero reservas y cero compras, y se eliminó
solo ese evento con predicados por ID/nombre y ausencia de reservas. El test se
ajustó para usar `psql -q` y para activar limpieza al detectar la inserción;
la segunda ejecución pasó.

Evidencia de la ejecución aprobada del evento sintético (antes → después):

- Eventos: stock `1 → 0`; reservas totales `0 → 1`; reservas confirmadas
  `0 → 1`; cantidad reservada confirmada `0 → 1`.
- Compras: confirmadas `0 → 1`; cantidad `0 → 1`; operaciones distintas
  `0 → 1`; filas COMMIT del journal `0 → 1`.
- Respuestas concurrentes: una `200` confirmada y una `409` por stock agotado.
- Tras el test, su limpieza selectiva dejó cero filas de evento, reservas y
  compras para el ID sintético. El evento residual de la primera ejecución
  también quedó eliminado con comprobación específica.

Comparación de solo lectura posterior a todas las regresiones: 17 compras
confirmadas con `operacion_id`, 17 reservas confirmadas, cero operaciones de
compra sin reserva, cero reservas sin compra y cero reservas pendientes. No se
ejecutó reconciliación automática. Las tres regresiones de idempotencia crean
compras QA en el evento 1 y esos registros quedan en staging; no se borraron.

La validación cubre PostgreSQL real compartido por ambas réplicas para
idempotencia y una carrera de sobreventa con claves distintas. No se invocó
`/compras/recuperar-servidor` con un caso pendiente real y no se simuló caída de
procesos; la recuperación real del endpoint sigue pendiente.

### Evidencia comunicada por el usuario

Se reportaron cuatro pruebas HTTP staging aprobadas en 6.59 s:
`test_shared_postgres_idempotency_across_both_purchase_replicas`,
`test_purchase_idempotency_replays_and_rejects_changed_request`,
`test_concurrent_purchase_retries_create_one_order` y
`test_oversell_attempt_is_rejected_without_changing_stock`. No se proporcionaron
logs ni repetí esas pruebas en esta auditoría; se identifican como evidencia
aportada, no como ejecución propia.

### Pruebas pendientes

- Salida abrupta de Compras2 en las mismas ventanas ya cubiertas para Compras1; fallos
  simultáneos y caídas de dependencias permanecen fuera de esta validación.
- Recovery desplegado que reconcilie un caso pendiente controlado y actualice
  el replay frente a PostgreSQL y Eventos reales.
- Compras1 sí se sometió a salida abrupta en cuatro puntos; ambas réplicas tienen
  además pruebas separadas de parada/restauración ordenada.
- DB Compras/Eventos y red Eventos indisponibles/restauradas.
- Nginx no disponible y health degradado con respuesta HTTP 200.
- Restore aislado del respaldo que se quiera presentar; el `verified` anterior
  es evidencia histórica, no revalidada aquí.
- Prueba de carga controlada con latencia, errores, pool y conexiones.

## 6. Plan incremental

| Prioridad | Mejora | Archivos probables | Pruebas y criterio de cierre | Riesgo/evidencia |
|---|---|---|---|---|
| P0 | **Implementado:** recuperación por `operacion_id`, lock NOWAIT, consulta de Eventos y reparación del replay sin migración nueva. | `compras/main.py`, `eventos/main.py`, pruebas | Regresiones unitarias aprobadas; la ruta recovery aún requiere ensayo real con un caso pendiente. | No es worker automático; claims sin reserva requieren retry/revisión. |
| P0 | **Validado en staging:** sobreventa concurrente con dos claves distintas y stock 1. | `tests/staging/test_auth_http.py` | Una compra/entrada confirmada, una reserva, stock 0, COMMIT único; test pasado. | Sin caída abrupta; las paradas ordenadas individuales de Compras1 y Compras2 están validadas en el apartado de failover. |
| P1 | Adaptar scripts de consistencia/caos a Compose staging, autenticación y keys. | `scripts/prueba_punto1_consistencia.py`, `scripts/prueba_punto3_tolerancia.py`, guía | Rechazan proyecto distinto de staging; prueban caída de ambas réplicas. | Prohibido ejecutar versiones actuales contra principal. |
| P1 | Reconciliación cruzada y fuente de stock controlada; mantener preview y aprobación explícita. | `eventos/main.py`, `compras/main.py`, pruebas, posible migración | Preview no escribe; bloquea discrepancias de compras/reservas y pendientes. | No imponer `EVENTOS_BASE` sobre inventario legítimamente alterado. |
| P1 | Healthchecks de app/readiness y retry POST selectivo solo donde exista idempotencia. | Compose y Nginx | Retira instancia realmente degradada; no repetir POST grupal sin key. | Routing/latencia y cambios de compatibilidad. |
| P2 | Migraciones versionadas para Eventos; quitar DDL/seed de startup gradualmente. | Eventos, Alembic, docs | Ensayo upgrade/rollback en copia restaurada. | Riesgo de catálogo/stock; requiere preflight. |
| P2 | Backup off-host y simulacro recurrente de restauración consistente. | Script/docs y operación | Hashes, estadísticas y manifest `verified`; RTO/RPO medidos. | Dumps de bases separadas no son snapshot global si siguen las escrituras. |
| P3 | Locust/capacidad antes de introducir infraestructura adicional. | Pruebas de carga/observabilidad | Objetivo throughput/p95/p99 y saturación cuantificados. | No incorporar Kafka/Kubernetes/Redis sin requisito medido. |

## 7. Backup y restauración

El procedimiento existente está en `docs/RESPALDO_RESTAURACION.md` y
`scripts/backup_main_databases.py`. Crear un backup consulta datos principales
y requiere ventana/permiso separado, por eso no se ejecutó. `verify` restaura a
una red Docker interna con `tmpfs`, verifica SHA-256, conteos y puede ensayar
migraciones en las copias.

Comando de ensayo solo con una ruta de backup revisada y aprobada:

```sh
python3 scripts/backup_main_databases.py verify \
  --backup-dir "$HOME/nexustix-backups/<directorio-aprobado>" \
  --rehearse-migrations
```

No pasar URLs principales a `verify`, no ejecutar restauración sobre volúmenes
principales y no considerar `pg_dump` suficiente: el manifiesto debe quedar
`verified` después de restaurar y comparar.

## 8. Comandos seguros para la siguiente etapa

Pruebas locales aisladas:

```sh
python -m pytest -q tests/test_authentication.py tests/test_backup_procedure.py
```

Pruebas HTTP con escrituras sintéticas, solo tras confirmar que el entorno es
staging y que los puertos corresponden a `18080–18085`:

La suite HTTP staging se invocó sin habilitar escritura: **21 skipped** en
0.21 s porque faltó `NEXUSTIX_STAGING_HTTP=1`. No son pruebas aprobadas.

Las pruebas de caída requieren ventana de staging; adaptar scripts para usar
explícitamente `-p nexustix-auth-staging -f docker-compose.staging.yml` antes
de detener servicios. No usar `down -v`, reset ni comandos de limpieza.

La prueba
`test_concurrent_different_buyers_cannot_oversell_synthetic_staging_event`
añadida crea dos compradores de QA y un evento con stock 1 dentro del Compose
staging explícito; compite con dos claves distintas, espera 200/409, y comprueba
stock, reserva confirmada, compra y COMMIT journal. Luego elimina únicamente
las filas vinculadas a ese evento, las dos claves y los correos generados. Si
falla la limpieza, conserva el evento sintético para diagnóstico; no ejecuta
limpieza amplia.

Comando previsto, solo tras confirmar que el staging ya está iniciado y sus
migraciones ya aplicadas:

```sh
export NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets"
NEXUSTIX_STAGING_HTTP=1 /tmp/nexustix-idempotency-test-venv/bin/python -m pytest -q -s \
  tests/staging/test_auth_http.py::test_concurrent_different_buyers_cannot_oversell_synthetic_staging_event
```

La prueba rechaza URLs que no sean HTTP local en los puertos 18081, 18082 y
18085. El acceso SQL usa `docker compose --project-name nexustix-auth-staging
-f docker-compose.staging.yml exec` y apunta solo a `usuarios_db`, `eventos_db`
y `compras_db` del staging. No levanta/detiene servicios ni corre migraciones.
Esta prueba HTTP no se ejecutó en esta iteración.

### Preparación para validar recovery real en staging — plan previo a escritura

La API actual de Eventos no crea eventos, por lo que la prueba controlada
insertará en `eventos_db` exactamente tres filas `eventos` con IDs aleatorios
del rango sintético `1_000_000_000–1_999_999_999`, nombre único
`nexustix-staging-recovery-<run-id>-<caso>` y `stock=2`. No se consultará ni
modificará el evento 1 u otros eventos. El alta usará únicamente la base
`eventos_db` del proyecto Compose `nexustix-auth-staging`.

Un proceso auxiliar en el contenedor ya activo `compras1` usará el código real
de compra y PostgreSQL compartido para producir estos escenarios. Los fallos
serán inyectados solo en el acuse HTTP del proceso auxiliar:

| Caso | Estado sintético que se generará | Estado inicial esperado | Resultado esperado |
|---|---|---|---|
| Replay pendiente | Eventos reserva y confirma; el helper pierde el acuse de confirmación después del commit remoto. | Compra persistida, reserva `confirmada`, claim `completed` con replay pendiente. | Recovery actualiza el replay con evidencia durable; la segunda llamada no duplica ni cambia stock. |
| Claim sin reserva | El helper simula timeout antes de enviar la solicitud de reserva. | Claim `processing`, sin compra ni reserva, stock 2. | Dos barridos globales lo diagnostican `claim_sin_reserva_reintentable`, sin mutarlo; retry de la misma clave completa una compra con el mismo `operacion_id`. |
| Reserva sin compra | Eventos reserva; el helper simula timeout después del commit remoto. | Claim `processing`, reserva `reservada`, sin compra, stock 1. | Tras superar naturalmente el umbral stale configurado, recovery libera una vez; la segunda llamada no modifica el stock. |

El endpoint de recovery no acepta un filtro por operación y procesa candidatos
globalmente. Por eso el helper comprueba antes de cada barrido que el conjunto
de candidatos sea exactamente el esperado (primer barrido: estas tres
operaciones; segundo: solo el claim sin reserva). Si detecta un candidato
ajeno, no envía la petición de recovery.

Las claves y operaciones se generarán por ejecución. El tiempo stale se
alcanzará esperando `PURCHASE_RECOVERY_STALE_SECONDS + 2`; no se cambiarán
timestamps ni estados internos por SQL. El token del administrador QA se leerá
localmente y pasará al helper solo por stdin. Cada login crea una sesión refresh
en `usuarios_db`, revocada mediante `POST /auth/logout`; sus filas de revocación
se conservarán como auditoría, incluidos los intentos anteriores del harness
que alcanzaron el login. Las llamadas de recovery serán HTTP
al Compras activo en `127.0.0.1:8003`. No se imprimirán el token ni respuestas
HTTP completas.

Limpieza prevista, solo tras cotejar estado y conteos exactos:

1. En `eventos_db`, borrar las tres reservas por sus `operacion_id` aleatorios
   y después borrar las filas de `eventos` por la pareja exacta `(id, nombre)`.
   Se exige que no haya dependencias adicionales y que stock final sea `1`, `1`
   y `2`, respectivamente.
2. En `compras_db`, borrar las dos compras por `operacion_id`, los tres claims
   por sus claves aleatorias, dos entradas `RECOVERY` por IDs exactos y las dos
   entradas `COMMIT`/una `CONFIRMACION_PENDIENTE` por `compra_id` u
   `operacion_id` sintético. Se exigen exactamente 2 compras, 3 claims y 5
   entradas de journal antes de limpiar. No se borran usuarios, compras
   históricas ni filas ajenas.
3. Si algún estado, conteo, journal o dependencia no coincide, no limpiar y
   conservar el escenario para diagnóstico. No se hará recovery ni limpieza
   contra el entorno principal, no se reiniciarán contenedores y no habrá caída
   de procesos.

El plan anterior se ejecutó en el Compose aislado. Los resultados y límites
observados se registran a continuación.

### Validación integrada de recovery en staging — 2026-10-23

**Entorno y preflight:** Compose `nexustix-auth-staging`, archivo
`docker-compose.staging.yml`, loopback staging `18080–18085`. `config --quiet`
aprobó; los servicios de las tres bases, Usuarios, Eventos, Compras1/2 y el
balanceador estaban activos. PostgreSQL fue `15.19` en las tres bases; Usuarios
estaba en Alembic `20261022_01` y Compras en `20261023_02`. El umbral real de
claims stale fue 120 s y se esperaron 122 s sin alterar timestamps. Health HTTP
de Usuarios, Eventos, ambas Compras y el balanceador devolvió 200; las tres
APIs reportaron `db=connected`.

**Prueba ejecutada:**

```sh
NEXUSTIX_STAGING_HTTP=1 \
NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets" \
NEXUSTIX_STAGING_ADMIN_EMAIL=qa-admin-staging@example.com \
NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE="$HOME/.config/nexustix-auth-staging-secrets/qa-admin-password" \
python3 -m pytest -q -s tests/staging/test_recovery_postgres_http.py
```

Resultado final: **1 passed in 129.33 s**. La prueba abrió una sesión con el
administrador QA existente, verificó su rol, hizo logout y comprobó que la
cookie refresh se revocó. El token de acceso y la contraseña no se registraron.
La fila de sesión refresh revocada se conserva como auditoría.

El helper de fallos controlados se ejecutó como proceso auxiliar dentro del
contenedor activo `compras1`, importó el código de compra vigente y usó sus
conexiones de PostgreSQL y las llamadas HTTP reales de Eventos. La recuperación
se invocó por HTTP autenticado contra `127.0.0.1:8003`; el endpoint respondió
HTTP 200 en ambos barridos. El cuerpo reportó `recuperacion_incompleta` en ambos
porque el claim sin reserva se conserva deliberadamente como reintentable.

| Escenario | Antes | Después del primer barrido | Segundo barrido y retry | Evidencia |
|---|---|---|---|---|
| Compra persistida, reserva confirmada, replay pendiente | Evento `1670578772`, compra `27`, claim `completed` con `confirmacion_reserva=pendiente`, reserva `confirmada`, stock 1. | Replay pasó a `confirmada`; misma compra 27, reserva confirmada y stock 1. | Segundo barrido no cambió nada. | Recovery HTTP 200; contador `replays_actualizados=1` en el primero y 0 en el segundo. |
| Claim antiguo sin reserva | Evento `1814073206`, claim `processing`, sin compra, consulta de reserva HTTP 404, stock 2. | Sin cambios; diagnóstico `claim_sin_reserva_reintentable`. | Segundo barrido repitió solo el diagnóstico; retry con la misma clave devolvió HTTP 200, creó compra `28`, una reserva confirmada y dejó stock 1 usando el mismo `operacion_id`. | `claims_sin_reserva=1` en cada barrido; no hubo compra ficticia antes del retry. |
| Reserva antigua sin compra | Evento `1416365523`, claim `processing`, reserva `reservada`, sin compra, stock 1. | Reserva pasó a `liberada`; claim finalizó con respuesta 409; sin compra y stock 2. | Sin cambios adicionales; no se repitió la liberación. | `reservas_liberadas=1` en el primer barrido y 0 en el segundo. |

Contadores del primer barrido: `replays_actualizados=1`,
`reservas_confirmadas=1`, `reservas_liberadas=1`, `claims_sin_reserva=1`,
`operaciones_ambiguas=0`, `omitidas_en_proceso=0`. En el segundo:
`replays_actualizados=0`, `reservas_confirmadas=0`, `reservas_liberadas=0`,
`claims_sin_reserva=1`, `operaciones_ambiguas=0`, `omitidas_en_proceso=0`.
El estado de cada fixture y stock se comparó entre ambos barridos.

**Limpieza:** guardas de ambas bases aprobaron antes de borrar. Se eliminaron
exactamente tres eventos, tres reservas, dos compras, tres claims y cinco filas
de journal (dos `RECOVERY`, dos `COMMIT`, una `CONFIRMACION_PENDIENTE`), con
predicados por IDs sintéticos, claves y operaciones. Después, las consultas
confirmaron cero eventos con el prefijo de prueba, cero claims/compras con el
sujeto sintético y cero filas de journal para los IDs capturados. Se revocó la
sesión refresh del administrador QA. Los contadores de secuencia de PostgreSQL
no se rebobinaron; los IDs asignados a filas ya limpiadas pueden dejar huecos.

Los intentos previos que llegaron a insertar fixtures crearon solo filas del
mismo prefijo sintético. Cada conjunto se inspeccionó y se limpió con
predicados exactos; no se ejecutó recovery dos veces por accidente y solo el
intento descrito arriba completó ambos barridos. Al cierre, los nueve servicios
de staging seguían activos. No se ejecutaron `stop`, `down`, migraciones ni
reconciliación sobre otros eventos. El entorno principal y sus volúmenes no se
consultaron ni modificaron.

**Límites:** los fallos de prueba fueron inyectados en el helper (antes de
reservar, después de que Eventos confirme una reserva y después de confirmar la
reserva pero perdiendo el acuse). La ruta de recovery sí se ejecutó por HTTP;
la preparación de compras invocó la función real del handler dentro del helper
con un `Principal` sintético para colocar esos estados, no envió un `POST
/compras` con JWT. Por ello esta prueba no vuelve a validar autorización de
compras (las pruebas HTTP de autenticación existentes cubren ese flujo). No se
probó una caída de proceso, una respuesta de estado de Eventos
indisponible/malformada, ni la denegación HTTP de un comprador en esta
ejecución. El código inspeccionado deja esos estados no verificables como
ambiguos y exige rol administrador, pero esos casos siguen pendientes de una
prueba integrada separada.

### Compra integral por HTTP en staging — 2026-10-23

Se añadió y ejecutó únicamente
`tests/staging/test_auth_http.py::test_http_purchase_idempotency_with_synthetic_event`
con `NEXUSTIX_STAGING_HTTP=1` y el directorio de secretos de staging. El
preflight pasó en modo Compose silencioso para `nexustix-auth-staging`, verificó
los puertos de APIs `18081–18085`, contenedores activos del proyecto esperado,
ambas revisiones Alembic (`usuarios=20261022_01`, `compras=20261023_02`) y
health HTTP 200 con `db=connected` para Usuarios, Eventos, Compras 1/2 y el
balanceador. El flujo de compra pasó por PostgreSQL real de staging y el
balanceador HTTP `:18085`.

La condición previa del catálogo se preparó directamente en `eventos_db`: un
evento con ID aleatorio `2088368290`, etiqueta QA única y stock inicial 3.
Esta inserción SQL fue solo preparación del fixture; la compra no se creó por
SQL. La identidad se registró por `POST /auth/register` (HTTP 201, rol
`comprador`) y se usó el JWT recibido en memoria. Después, el recorrido fue:

| Operación HTTP | Resultado | Comprobación persistida |
|---|---:|---|
| Compra inicial `POST /compras` con JWT y `Idempotency-Key` | **200** | Compra confirmada, reserva confirmada, claim `completed`, misma `operacion_id`, una fila COMMIT; stock 3→2. |
| Consulta de stock de Eventos y consulta interna de reserva | **200 / 200** | Eventos reportó stock 2 y la reserva vinculada al `operacion_id` indicó cantidad 1 y estado `confirmada`. |
| Repetición con la misma clave y cuerpo | **200**, `Idempotency-Replayed: true` | Mismo ID de compra y `operacion_id`; continuó existiendo una compra, una reserva y un claim; stock siguió en 2. |
| Repetición con la misma clave y cantidad distinta | **409** | Rechazo por conflicto; sin cambio en stock ni filas de compra/reserva. |
| Cierre de sesión | **200** | Refresh session revocada antes de eliminar el usuario sintético. |

La prueba comparó el cuerpo completo del replay con el cuerpo original y la
asociación `auth_subject`/`usuario_id` de compra y claim con el sujeto del JWT.
La limpieza validó el snapshot antes de borrar: solo el evento QA con stock 2,
una reserva confirmada, una compra confirmada, un claim completado, un COMMIT
del journal y una sesión refresh revocada del usuario generado. Eliminó esas
filas mediante IDs, clave, operación, subject, correo y estado exactos. Las
consultas posteriores dentro del helper comprobaron que no quedaran evento,
reserva, compra, claim, journal ni sesión para ese fixture. Resultado final de
la versión reforzada: **1 passed in 6.00 s**; sin omisiones.

La primera ejecución de esta nueva prueba completó correctamente registro,
compra, repetición y conflicto, pero falló al limpiar porque una guarda SQL
contaba por error la compra propia del escenario como una compra adicional del
usuario. No se borró nada en esa ejecución. Se consultaron las filas por
lectura, se comprobó que eran exactamente un escenario sintético íntegro (una
compra/reserva/claim/journal y una sesión ya revocada), se corrigió el predicado
y se ejecutó una limpieza puntual protegida por precondiciones. Una ejecución
intermedia corregida pasó en 5.58 s con el evento `2043218561` y limpió sus
propias filas. La versión final reforzada se volvió a ejecutar con el evento
`2088368290` para incluir las comprobaciones del cuerpo completo y del sujeto,
y pasó en 6.00 s. La primera ejecución fallida duró 4.39 s; su evento
`2057200913`, compra 29 y usuario 76 se inspeccionaron y limpiaron con filtros
exactos. La consulta final de solo lectura confirmó cero filas de evento,
reserva, compra, claim y sesión para los escenarios fallido y final; la
ejecución intermedia verificó cero filas en su limpieza. No se imprimieron
JWT, cookies ni claves internas.

**Límites:** la creación del evento de prueba requiere SQL porque Eventos no
ofrece una ruta de creación de eventos. Registro, compra, replay, rechazo,
consultas de stock/reserva y logout sí fueron HTTP. Esta prueba no valida
concurrencia entre réplicas, caída de procesos, recuperación de claims
ambiguos ni tolerancia a fallos del balanceador. No se tocaron el evento 1,
compras QA preexistentes, migraciones, el entorno principal ni sus volúmenes.

### Failover controlado de Compras1 en staging — 2026-10-23

La inspección confirmó que el proyecto era `nexustix-auth-staging`, el archivo
`docker-compose.staging.yml`, los nueve contenedores estaban activos y los
bindings coincidían con `18080–18085`; las tres bases estaban `healthy`. También
pasaron `docker compose ... config --quiet`, las revisiones de Usuarios y
Compras, y health HTTP 200 con `db=connected`. El Compose define exactamente
`compras1` y `compras2`. El test solo llama a `stop compras1` y `start compras1`
con el mismo `--project-name` y `-f`; no elimina ni recrea contenedores, no toca
volúmenes y no detiene los otros ocho servicios.

Configuración relevante de Nginx (`compras_lb/nginx.conf`): round robin a
`compras1:8003` y `compras2:8003`, `max_fails=2`, `fail_timeout=10s`, timeout de
conexión de 2 s y hasta dos intentos para errores de conexión/timeouts/502/503/504.
No habilita `non_idempotent`, por lo que este resultado no demuestra reenvío de
un POST cuyo cuerpo ya haya sido entregado al upstream; demuestra el caso
ensayado en que una réplica no estaba disponible y el cliente reusó la misma
clave.

Comando de prueba ejecutado:

```sh
NEXUSTIX_STAGING_HTTP=1 \
NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets" \
/tmp/nexustix-idempotency-test-venv/bin/python -m pytest -q -s \
  tests/staging/test_purchase_failover_http.py::test_http_purchase_continues_when_compras1_stopped
```

El test ejecuta explícitamente, siempre con el proyecto y archivo staging:

```sh
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop compras1
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml start compras1
```

| Momento | Evidencia observada |
|---|---|
| Antes | Nueve servicios activos; las tres DB saludables; health de APIs 200. Se preparó por SQL solo un evento sintético `2032461028`, stock 4, y un comprador QA. La compra baseline y su replay por HTTP al balanceador respondieron **200**; stock 4→3 y quedaron una compra, claim, reserva y COMMIT. |
| Durante la parada de Compras1 | Compose mostró `compras1` detenido y los otros ocho servicios activos. Compras2 respondió health **200**, `instancia=2`, DB conectada. El probe `/health` por el balanceador respondió **200**. El segundo `POST /compras`, con clave diferente, respondió **200** en **2323.6 ms**; el replay con la misma clave/cuerpo respondió **200** y `Idempotency-Replayed: true`. |
| Tras iniciar Compras1 | El contenedor volvió a `running`; health directo de Compras1 y Compras2: **200**, `db=connected`. Nginx volvió a responder alternadamente desde las instancias `1` y `2`. Las dos compras, dos reservas confirmadas, dos claims y dos COMMIT permanecieron sin cambios; stock final 2. Repetir ambas claves después de recuperar volvió a responder con replay, sin nuevos débitos. |

La interrupción medida entre la finalización de `stop` y `start` fue **5.101 s**.
Durante el POST no se registró un error HTTP visible al cliente; la latencia de
2.324 s es consistente con un intento de conexión al upstream detenido y el
reenvío al superviviente. Antes de limpiar, las guardas confirmaron que el
evento tenía stock 2, dos reservas confirmadas, dos compras confirmadas del
comprador sintético, dos claims completados con sus `operacion_id` y dos filas
COMMIT. La prueba limpió solo esas filas y el usuario/sesión QA. La consulta
final de solo lectura encontró cero eventos/reservas, compras/claims/journal
para el fixture. Todos los nueve servicios quedaron activos; no se cambió
ningún evento real ni el entorno principal.

Un primer intento de la prueba terminó antes de la compra durante la caída por
una aserción que esperaba `instance` cuando el endpoint devuelve `instancia`.
El `finally` restauró `compras1`. Ese intento dejó solo su compra baseline
confirmada; se verificó y limpió con filtros exactos antes de corregir el campo.
La primera invocación de esa limpieza fue rechazada por expansión de `$$` en el
shell y no ejecutó SQL; se repitió con argumentos de proceso, con precondiciones
y conteos posteriores. La ejecución corregida pasó: **1 passed in 16.23 s**.

**Límites:** se detuvo ordenadamente el contenedor `compras1`; no se mató el
proceso abruptamente, no se detuvo Compras2 y no se midió pérdida/duplicación
ante una caída ocurrida después de recibir el POST. Tampoco se ensayaron fallos
de Nginx, Eventos, usuarios, PostgreSQL o el host. La tolerancia medida es la
de una réplica de Compras mientras su balanceador y dependencias siguen activos;
no es alta disponibilidad integral.

### Failover controlado de Compras2 en staging — 2026-10-23

Se ejecutó el wrapper `test_http_purchase_continues_when_compras2_stopped` de
`tests/staging/test_purchase_failover_http.py`, que reutiliza las mismas
precondiciones estrictas y limpieza selectiva del escenario de Compras1. El
preflight verificó `docker compose ... config --quiet`, el proyecto exacto
`nexustix-auth-staging`, los nueve servicios requeridos, puertos loopback
`18080–18085`, las tres bases `healthy`, revisiones Alembic Usuarios
`20261022_01` y Compras `20261023_02`, y health HTTP 200 con DB conectada. No
consultó el proyecto principal.

Comando ejecutado:

```sh
NEXUSTIX_STAGING_HTTP=1 \
NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets" \
/tmp/nexustix-idempotency-test-venv/bin/python -m pytest -q -s \
  tests/staging/test_purchase_failover_http.py::test_http_purchase_continues_when_compras2_stopped
```

El test usó exclusivamente estos comandos ordenados, con `--project-name
nexustix-auth-staging -f docker-compose.staging.yml`, para detener y restaurar
la réplica objetivo:

```sh
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop compras2
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml start compras2
```

| Momento | Evidencia observada |
|---|---|
| Antes | Preflight de nueve servicios y tres bases saludables aprobado. Se creó un evento aislado vía SQL en `eventos_db`, stock 4, más un comprador QA por registro HTTP. Compra baseline por el balanceador y replay de la misma clave/cuerpo respondieron **200**; stock 4→3. |
| Durante la parada ordenada de Compras2 | Solo `compras2` se detuvo; Compras1 permaneció activa y su health directo respondió **200** (`instancia=1`, DB conectada). El balanceador respondió **200** dirigiendo el probe a la instancia superviviente. El POST con segunda clave respondió **200** en **2286.9 ms** y su replay **200**. El stop→start duró **5.658 s**. |
| Tras restaurar Compras2 | Health directo de Compras1 y Compras2: **200**; el balanceador volvió a mostrar ambas instancias (`2`, `1`). Replay de las claves conservó sus resultados. El stock pasó a 2 por las dos compras únicas y no volvió a cambiar por replays ni por la restauración. |

La salida de pytest fue **1 passed in 16.85 s**. El fixture fue el evento
sintético `2092108739`; las compras de prueba fueron IDs `35` y `36` con dos
`operacion_id` distintos. Antes de limpiar, las consultas de solo lectura
encontraron exactamente dos compras confirmadas, dos claims `completed`, dos
reservas `confirmada`, dos COMMIT de journal y stock 2. La limpieza verificó
propietario/sujeto, evento, claves, estados y operaciones exactos; eliminó solo
esas filas, las reservas/evento fixture y el comprador/sesión sintéticos. La
comprobación posterior obtuvo cero filas relacionadas con el escenario. No se
modificó inventario ni se borraron filas ajenas. El `docker compose ps` final
confirmó los nueve servicios `running` y las tres bases `healthy`.

Esta evidencia es de **parada ordenada** de Compras2, no de `kill`, caída del
proceso durante una solicitud ni fallo después de recibir/confirmar un POST.
No se detuvieron otros servicios. La latencia observada incluye el tiempo de
fallo de conexión/reintento del balanceador; no se vio error HTTP al cliente.
La prueba no demuestra alta disponibilidad integral: Nginx, Eventos, Usuarios,
las bases de datos y el host siguen siendo puntos únicos de fallo.

### Caída abrupta durante compra en staging — 2026-10-23

La implementación previa no ofrecía checkpoints que pudieran sincronizar una
terminación abrupta con una fase exacta de la solicitud. Se añadió un hook de
prueba en `compras/main.py`, activado únicamente cuando
`NEXUSTIX_DEPLOYMENT_ENV=staging`, `NEXUSTIX_TEST_CRASH_HOOKS=1` y el request
incluye `X-NEXUSTIX-Test-Crash-At` con un checkpoint admitido. La bandera de
fallo vale `0` por defecto en `docker-compose.staging.yml`; la suite la habilita
solo para `compras1`, lo recrea con `--no-deps` y al terminar lo vuelve a
recrear con el hook apagado. El código usa `os._exit(86)` únicamente en ese
camino de test.
No se añadió un endpoint público de administración de fallos ni se habilitó
esta variable en el Compose principal.

El test `test_abrupt_purchase_process_crash_recovers_idempotently` de
`tests/staging/test_purchase_abrupt_crash_http.py` usa fixtures independientes
por escenario: comprador registrado por HTTP, evento nuevo con stock 2
insertado directamente en `eventos_db` (no hay endpoint de creación), JWT en
memoria y una `Idempotency-Key` única. Envía el POST inicial directamente a
`http://localhost:18083/compras` para seleccionar Compras1 sin ambigüedad; el
primer cliente recibe `ConnectionError` cuando el proceso termina. Después
espera el reinicio automático `on-failure`, verifica `RestartCount`, compara
los IDs y estados de los nueve contenedores para probar que los otros ocho no
fueron reemplazados, y reintenta por el balanceador con el mismo JWT, cuerpo y
clave. No invoca `/compras/recuperar-servidor`, que podría barrer claims ajenos.

La suite se ejecutó con:

```sh
NEXUSTIX_STAGING_HTTP=1 \
NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets" \
/tmp/nexustix-idempotency-test-venv/bin/python -m pytest -q -s \
  tests/staging/test_purchase_abrupt_crash_http.py
```

Cada caso verificó su snapshot en PostgreSQL/Eventos antes del retry, luego una
sola compra, un claim completado, una reserva confirmada, stock final 1, un
COMMIT del journal y un replay HTTP 200 sin efectos adicionales. La limpieza
comparó propietario, evento, key, operación, estados y stock exactos; borró
solo filas de ese fixture y confirmó conteos cero antes de continuar.

| Escenario y punto del proceso terminado | Estado persistido al perder conexión | Retry y replay | Reinicio Compras1 |
|---|---|---|---|
| A. `before_claim` | Sin claim, compra ni reserva; stock 2. No se había asignado un `operacion_id` durable. | Retry HTTP **200** crea una sola operación; replay **200**. La igualdad con un ID anterior no aplica porque no existía. | `RestartCount` 0→1; Compras1 y Compras2 health **200**. |
| B. `after_claim` | Claim `processing`; sin compra ni reserva; stock 2. | Retry HTTP **200** y replay **200**; conserva el `operacion_id` del claim. | `RestartCount` 1→2; ambas health **200**. |
| C. `after_reservation` | Claim `processing`, sin compra, reserva `reservada`; stock 1. | Retry HTTP **200** reutiliza el mismo `operacion_id`; una reserva termina `confirmada`, una compra; replay **200**, stock sigue 1. | `RestartCount` 2→3; ambas health **200**. |
| D. `after_final_response_commit` | Compra y respuesta idempotente ya persistidas, reserva `confirmada`, claim `completed`; stock 1. El cliente no recibió la respuesta. | Retry HTTP **200** con replay; replay siguiente **200**, misma compra/operación e inventario. | `RestartCount` 3→4; ambas health **200**. |

Resultado final de pytest: **4 passed in 60.21 s**. Los cuatro primeros requests
terminaron con `ConnectionError`; los reintentos y replays respondieron 200.
Las duraciones medidas desde antes del request abrupto hasta la verificación y
limpieza fueron **6.146 s**, **5.983 s**, **6.908 s** y **6.606 s** (A–D).
En cada fase el conteo final fue una compra, una reserva, un claim y un COMMIT;
stock `2 → 1` una sola vez. La suite borró los cuatro eventos, usuarios,
sesiones, compras, claims, reservas y filas COMMIT sintéticos con predicados
exactos. Consultas posteriores encontraron cero fixtures con esos prefijos.
Compras1 quedó `running` con `NEXUSTIX_TEST_CRASH_HOOKS=0`; las nueve instancias
quedaron activas y las tres bases saludables. No se expusieron credenciales,
JWT ni secretos.

Una invocación inicial de la suite no devolvió su resumen de salida a la
consola. Durante el diagnóstico solo de lectura apareció temporalmente un claim
sintético `processing` sin evento, compra, reserva ni usuario asociado; no se
ejecutó recovery ni borrado. En la reconsulta posterior el claim ya no existía.
La ejecución final, iniciada después de comprobar los prefijos en cero, sí
produjo el resultado reproducible de cuatro casos y limpieza verificada.

**Límites:** A demuestra caída antes de que exista claim durable, por lo que no
hay ID de operación previo que conservar. D cubre la caída después de persistir
la respuesta final y antes de enviarla; no cubre el intervalo más estrecho entre
el commit local de la compra y la confirmación/actualización final en Eventos.
La caída abrupta se probó solo en Compras1. Compras2 se validó con parada
ordenada, no una señal `SIGKILL` al contenedor. No se probaron caídas de Nginx,
Eventos, Usuarios, PostgreSQL ni host. Esto no acredita alta disponibilidad
integral.

### Intento de caída abrupta de Compras2 en staging — 2026-10-24

Se inspeccionaron `_test_crash_at` en `compras/main.py`, el Compose staging y la
suite `tests/staging/test_purchase_abrupt_crash_http.py`. Los checkpoints son
`before_claim`, `after_claim`, `after_reservation` y
`after_final_response_commit`. El hook solo se activa con deployment `staging`,
la bandera explícita en `1` y el encabezado de checkpoint correspondiente.
La imagen existente ya contenía este hook y no se reconstruyó.

El contenedor activo de Compras2 no tenía las variables explícitas del hook.
Se añadió el mismo cableado de staging que Compras1, con hook `0` por defecto,
y se recreó únicamente Compras2 con esa configuración apagada. La prueba se
parametrizó por réplica. Se añadió también una variable de política de reinicio
con valor normal `on-failure:5`; durante el caso se intenta crear solo la
réplica objetivo con política `no`, sin cambiar la configuración principal.

El preflight pasó antes de las ejecuciones: `config --quiet`, nueve servicios
del proyecto `nexustix-auth-staging`, puertos loopback `18080–18085`, tres bases
`healthy`, revisiones Alembic esperadas y health HTTP 200 de frontend, Usuarios,
Eventos, Compras1, Compras2 y balanceador. El primer intento desactivó el
reinicio automático mediante `docker update --restart=no`; el segundo lo
declaró en el Compose solo al recrear el objetivo. En ambos, el POST directo
perdió la conexión y el estado persistido coincidió con el checkpoint, pero
`docker compose ps` reportó Compras2 todavía `running`. La aserción abortó antes
de probar el retry, replay o recuperación. Ningún escenario se considera
aprobado.

| Escenario | Snapshot observado tras perder la conexión | Resultado |
|---|---|---|
| A. Antes del claim | Sin claim, compra ni reserva; stock 2. | Fallido/inconcluso: el contenedor seguía `running`; sin retry. |
| B. Después del claim | Claim `processing`, sin compra ni reserva; stock 2. | Fallido/inconcluso: el contenedor seguía `running`; sin retry. |
| C. Después de reservar | Claim `processing`, reserva `reservada`, sin compra; stock 1. | Fallido/inconcluso: el contenedor seguía `running`; sin retry. |
| D. Tras persistir compra/respuesta | Claim `completed`, una compra, reserva `confirmada`; stock 1. | Fallido/inconcluso: el contenedor seguía `running`; sin replay. |

Las dos ejecuciones terminaron con **4 failed, 4 deselected**: 62.47 s y
60.28 s. Se inspeccionaron los ocho fixtures por IDs, claves, operaciones,
cuentas y dependencias sintéticas; no se invocó recuperación. Sus estados
coincidían con los checkpoints esperados y se limpiaron exclusivamente con
guardas exactas: ocho eventos, cuatro reservas, seis claims, dos compras, dos
COMMIT del journal y ocho usuarios/sesiones de prueba. Las consultas posteriores
devolvieron cero filas para esos IDs y cuentas.

Al terminar, los nueve servicios quedaron `running`, las tres bases `healthy`,
los seis puertos de staging conservaron sus valores, todos los health HTTP
respondieron 200 y Compras1/2 mostraron el hook apagado. No se consultó ni
modificó el entorno principal.

**Límite:** se observaron los estados durables del punto de fallo, pero no se
demostró que el proceso/contenedor de Compras2 permaneciera detenido; tampoco se
probó la continuidad del balanceador durante una indisponibilidad ni se
ejecutaron retries para estos casos. La causa de que el contenedor apareciera
`running` queda sin determinar. Diagnosticar este punto antes de repetir una
caída o afirmar failover abrupto de Compras2. Esto no prueba caída física del
host ni de PostgreSQL.

### Diagnóstico de por qué Compras2 aparece `running` — 2026-10-24

Se realizó inspección de solo lectura después de esos intentos; no se repitió
ningún checkpoint ni se enviaron compras. Compose `config --quiet` pasó. La
configuración normal resuelta conserva `restart: on-failure:5` y deja el hook
deshabilitado por defecto. La misma configuración resuelta con los overrides
que usa el fixture (`NEXUSTIX_TEST_CRASH_HOOKS=1` y política `no`) produce
`restart: no` y hook `1`; esto verifica la interpolación estática, no prueba qué
configuración tuvo el contenedor durante los intentos anteriores.

La comprobación HTTP directa actual identifica `instancia=1` en `18083` e
`instancia=2` en `18084`; ambos `/health` respondieron 200 con DB conectada. El
balanceador también respondió 200. Esto descarta una confusión actual de puertos
o identidad, pero no conserva la identidad de las solicitudes fallidas. Los
contenedores actuales comparten imagen y ejecutan directamente
`uvicorn main:app`; `docker inspect` muestra `Entrypoint=null`, y `/proc/1`
identifica Uvicorn como PID 1 en ambas réplicas. No hay un supervisor de
aplicación visible que pueda mantener vivo el contenedor tras `os._exit(86)`.
El hook sigue condicionado a `NEXUSTIX_TEST_CRASH_HOOKS=1`,
`NEXUSTIX_DEPLOYMENT_ENV=staging` y al checkpoint recibido. En los contenedores
activos, el cálculo booleano de activación dio falso para ambos servicios.

Los metadatos de los contenedores actuales son `running`, `ExitCode=0`,
`RestartCount=0` y política Docker `on-failure`; sus tiempos de inicio son
posteriores a los intentos y Compras2 tiene un contenedor nuevo. Esto no revela
el estado del contenedor que atendió cada POST. El fixture restaura la réplica
con `up --force-recreate` en `finally`, lo que reemplaza el contenedor de la
prueba y elimina la posibilidad de inspeccionar después su `State`, código de
salida y logs originales. Los logs disponibles solo contienen el arranque de
los contenedores actuales; el flujo del checkpoint no registra una línea antes
de llamar `os._exit`. La consulta del flujo de eventos Docker disponible no
devolvió eventos `die`/`start` asociados a las pruebas (solo un `exec_die` de
una consulta posterior), por lo que tampoco permite reconstruirlas.

**Conclusión:** se descarta que la imagen actual tenga un supervisor entre
Uvicorn y PID 1, y la prueba apunta directamente al puerto de la réplica
objetivo. Los snapshots previos de claim/reserva son compatibles con que cada
checkpoint haya sido alcanzado. Sin el ID, la política efectiva, `StartedAt`,
`RestartCount` y eventos del contenedor en el instante del fallo, no se puede
determinar por qué el contenedor observado entonces seguía `running`. La causa
no queda demostrada y no se cambió código ni prueba en esta inspección. Los
cuatro escenarios abruptos de Compras2 permanecen pendientes; no deben
reintentarse hasta que una validación determinista conserve e inspeccione el
contenedor objetivo antes de que el fixture lo reemplace.

Al inicio y al cierre de la inspección, ambos hooks estaban deshabilitados en
las réplicas activas. No se modificó el entorno principal ni se alteraron
servicios o datos de staging.

## 9. Criterios objetivos de cierre

- **Idempotencia:** mismo key/sujeto/cuerpo por cada réplica y concurrente da
  una compra/operación/respuesta; cambiar sujeto o cuerpo devuelve 409.
- **Ambigüedad:** timeout y caída tras reserva conservan `operacion_id`; no hay
  liberación hasta verificar compra; recovery es repetible y actualiza replay.
- **Inventario:** distintas keys concurrentes no superan stock y la ecuación de
  inventario y correlación de reservas coincide.
- **Failover:** detener cada Compras por separado mantiene servicio por la otra;
  retry de resultado desconocido con la misma clave no duplica.
- **Dependencias únicas:** documentar interrupción por DB, Eventos, Nginx/host;
  no declarar HA de esos componentes hasta implementar y probar redundancia.
- **Backup:** restore aislado, hashes y conteos coinciden, ensayo migratorio
  termina, evidencia permanece guardada.
- **Carga:** throughput, latencias, errores, conexiones y recursos observados
  bajo una meta cuantificada.

## 10. Archivos inspeccionados

- `compras/main.py`, `compras/models.py`, `compras/database.py`, migraciones de
  Compras `20261022_00`, `20261022_01`, `20261023_02`.
- `eventos/main.py`, `eventos/models.py`, `eventos/database.py`.
- `usuarios/main.py`, `usuarios/models.py`, `usuarios/database.py` y sus dos
  migraciones.
- `docker-compose.yml`, `docker-compose.staging.yml`, `compras_lb/nginx.conf`,
  `frontend/nginx.staging.conf`.
- Tests de autenticación, HTTP staging, frontend y backup; scripts de prueba,
  backup y reset.
- Documentación de despliegue, staging, consistencia, recuperación, backup y
  demo distribuida.

No se imprimieron secretos, contraseñas, cookies ni tokens. Para autenticar la
prueba de recovery anterior, el runner leyó la contraseña QA en memoria desde
su archivo protegido y envió el access token al helper solo por stdin. En la
prueba de caída actual se usaron compradores sintéticos registrados por HTTP;
los JWT quedaron en memoria. Las consultas y escrituras SQL se dirigieron
explícitamente a `nexustix-auth-staging`. Para esta prueba se construyó la imagen
actualizada de Compras1 y se recreó solo ese servicio dos veces: para habilitar
el hook de crash durante la suite y para restaurarlo con el hook `0`. Las cuatro
terminaciones abruptas ocurrieron exclusivamente en Compras1. No se consultaron
ni cambiaron bases, volúmenes, secretos o servicios principales, y no se
ejecutaron migraciones ni limpiezas generales.

## 11. Indisponibilidad de Eventos y operaciones ambiguas — 2026-10-23

### Alcance y preflight

Se validó exclusivamente `nexustix-auth-staging` con
`docker-compose.staging.yml`. `config --quiet` terminó correctamente; el
proyecto tenía nueve servicios, publicaciones loopback en `18080–18085`, las
tres bases PostgreSQL 15 saludables y revisiones Usuarios `20261022_01` y
Compras `20261023_02`. Las rutas HTTP de frontend, Usuarios, Eventos, Compras1,
Compras2 y balanceador respondieron HTTP 200 con `db=connected` en las APIs.
Al terminar, los nueve servicios seguían activos; Eventos había sido iniciado
de nuevo y todas las APIs volvieron a responder HTTP 200. Los IDs de los
contenedores ajenos a Eventos no cambiaron.

### A. Eventos detenido antes de aceptar la reserva — prueba HTTP real

Prueba: `tests/staging/test_events_unavailability_http.py::test_purchase_retries_safely_after_events_unavailable_before_reservation`.
Resultado final: **1 passed en 21.58 s**. Una ejecución anterior también pasó
en 22.87 s. Al añadir la medición de disponibilidad, una repetición falló antes
de insertar el fixture porque su ID aleatorio excedía `integer` de PostgreSQL;
se acotó el rango, se confirmó que no hubo inserción ni parada de servicios y la
ejecución final pasó. El fixture fue un evento sintético con stock
2 y un comprador QA registrado por HTTP. El evento se insertó directamente en
la base aislada de Eventos porque no hay un endpoint público de creación
habilitado para ese uso; esa preparación no fue una compra.

- Se detuvo solo el servicio Compose `eventos`; Usuarios, Compras1/2, Nginx,
  frontend y las tres bases permanecieron activos. El test restableció Eventos
  en `finally` y esperó health con DB conectada.
- La compra inicial, enviada por HTTP con JWT y `Idempotency-Key` al balanceador,
  respondió **503** en **7711.9 ms**. Claim: `processing` con `operacion_id`
  persistido; compras 0; reservas 0; stock 2. No se liberó inventario.
- Después de restaurar Eventos, la misma identidad, cuerpo y clave se reintentó
  por HTTP: **200**, compra confirmada y reserva confirmada, stock 1, un claim
  completado y un COMMIT del journal. El `operacion_id` coincide en claim,
  compra y reserva.
- Replay de la solicitud: **200** con `Idempotency-Replayed: true`; los
  snapshots antes/después fueron iguales.
- La prueba limpió solamente el evento, reserva, compra, claim, COMMIT y usuario
  QA identificados. Las precondiciones exactas se verificaron y las consultas
  posteriores confirmaron cero filas de ese fixture.

La indisponibilidad medida desde que `docker compose stop eventos` terminó
hasta que `/health` volvió a dar 200 con `db=connected` fue **13055 ms**. El HTTP
503 tardó **7711.9 ms**, consistente con el timeout interno de Compras a Eventos
más el recorrido por el balanceador. Esto fue una parada ordenada del servicio
de aplicación, no una caída abrupta ni una interrupción de `eventos_db`.

### B. Reserva aceptada, respuesta perdida — PostgreSQL y Eventos reales

Prueba: `tests/staging/test_recovery_postgres_http.py::test_recovery_reconciles_real_postgres_and_events_without_duplicate_effects`.
Resultado: **1 passed en 130.22 s**. El helper ejecutó la función de compra de
Compras1 con PostgreSQL real; para el punto de fallo dejó que la llamada HTTP
real a Eventos terminara y recibiera el 200 de reserva confirmada por su
transacción, y solo después sustituyó el ACK por `ReadTimeout`. No se detuvo
Eventos; esta es una inyección controlada de pérdida de respuesta, no una caída
real del servicio. El test esperó 122 s, que supera el stale threshold real de
120 s, sin alterar timestamps.

En el fixture correspondiente, antes de recovery había claim `processing`,
cero compras, reserva `reservada` y stock 1 (stock inicial 2). Recovery consultó
la reserva real por `operacion_id`, verificó que no había compra y que el claim
era antiguo, y la liberó: después había cero compras, reserva `liberada`, stock
2 y claim finalizado con respuesta 409. La segunda ejecución de recovery no
volvió a liberar ni cambió stock. El mismo `operacion_id` no se reutiliza tras
liberación; un nuevo intento de compra requeriría una clave nueva.

La misma suite también cubrió dos fixtures independientes: (1) compra
persistida/reserva confirmada con replay aún `pendiente`, que quedó corregido a
`confirmada`; (2) claim antiguo sin reserva, diagnosticado como
`claim_sin_reserva_reintentable` sin crear compra ficticia. La segunda pasada no
repitió efectos. El retry del segundo fixture usó la misma clave y produjo una
compra/reserva, conservando el `operacion_id`. El resumen del runner mostró
primera recuperación HTTP 200 con un replay actualizado y una reserva liberada;
segunda recuperación HTTP 200 sin efectos adicionales. El estado global fue
`recuperacion_incompleta` solo porque el claim sin reserva quedó correctamente
pendiente de retry.

Se limpiaron exactamente 3 eventos, 3 reservas, 2 compras, 3 claims y 5 filas
de journal de recovery/COMMIT; el test confirmó las guardas y revocó la sesión
refresh del administrador QA. No se ejecutó recovery sobre operaciones ajenas.

### C. Respuesta de reserva no parseable — doble HTTP aislado

Prueba unitaria: `tests/test_authentication.py::test_unverifiable_events_reservation_response_keeps_claim_ambiguous`.
Resultado: **1 passed en 0.95 s** en SQLite aislado. Un doble devolvió HTTP 200
pero lanzó `ValueError` al parsear JSON. El claim durable quedó `processing`,
sin respuesta cacheada y sin compra; el doble no contactó ni modificó Eventos.
La excepción sale actualmente sin conversión explícita en `crear_compra`; por
ello la prueba unitaria observa `ValueError`. En la ruta ASGI normal se espera
HTTP 500 por error no manejado, pero este código HTTP no se midió contra
staging. La operación debe tratarse como ambigua: no se confirmó ni liberó
reserva.

La suite unitaria completa `tests/test_authentication.py` terminó con **20
passed en 4.95 s**; el módulo staging nuevo pasó `py_compile`. No se contó
ningún caso omitido como aprobado.

### Comportamiento inspeccionado y límites

- Conexión rechazada o timeout durante `POST /reservar`: Compras devuelve 503,
  conserva el claim `processing` y su `operacion_id`; no crea compra ni llama a
  compensación. El escenario A verificó connection refusal real.
- Eventos responde 5xx: Compras propaga ese status y conserva el claim para
  retry/recovery, porque Eventos pudo haber comprometido la reserva antes del
  error. Se verificó por inspección de `compras/main.py`, pero no se inyectó un
  HTTP 5xx real en esta ejecución.
- JSON no parseable: la excepción de `.json()` no se captura en el paso de
  reserva; la prueba unitaria confirmó claim `processing` y ausencia de compra.
  Falta convertirlo explícitamente en 503 ambiguo y probar el status HTTP.
- Una respuesta JSON sintácticamente válida pero con `operacion_id`, evento,
  cantidad o estado incorrectos no se valida antes de persistir la compra
  (`compras/main.py`, alrededor de `reserva_data = resp_reserva.json()`). Es un
  riesgo de integridad pendiente: hace falta validar correlación/esquema y
  mantener la operación ambigua si no coincide. No se envió tal payload falso
  contra servicios desplegados.
- No se probaron caída abrupta de Eventos, HTTP 5xx real, DB Eventos caída,
  respuesta semánticamente incorrecta, pérdida de PostgreSQL ni fallos de host.
  No se probó alta disponibilidad integral.

Archivos añadidos/modificados para esta validación: el nuevo test
`tests/staging/test_events_unavailability_http.py`, el caso aislado en
`tests/test_authentication.py` y esta sección. No se modificó el backend,
Compose, imágenes, esquema ni datos principales.

## Endurecimiento del contrato Compras–Eventos — 2026-10-23

Esta sección actualiza y reemplaza, para el código vigente, las observaciones
anteriores de la sección previa sobre respuestas remotas no parseables o sin
correlación. Se modificaron `compras/main.py` y `eventos/main.py`; no se añadió
migración.

### Contrato que Compras valida

- `POST /eventos/{evento_id}/reservar`: objeto JSON con `operacion_id`,
  `evento_id`, `cantidad`, `estado_reserva="reservada"`, entero no negativo
  `stock_restante`, booleano `idempotente` y entero no negativo
  `reloj_logico_T`. La identidad de operación, evento y cantidad debe coincidir
  exactamente con la solicitud.
- `POST /eventos/{evento_id}/confirmar`: objeto con `status="ok"`, los mismos
  identificadores y cantidad, `estado_reserva="confirmada"`, booleano
  `idempotente` y reloj lógico entero no negativo.
- `POST /eventos/{evento_id}/liberar`: objeto con los mismos identificadores y
  cantidad, `estado_reserva="liberada"`, stock no negativo, booleano
  `idempotente` y reloj lógico entero no negativo.
- `GET /eventos/reservas/{operacion_id}`: evidencia durable con operación,
  evento, cantidad, estado (`reservada`, `confirmada` o `liberada`) y
  `stock_actual` no negativo. Se usa para resolver respuestas perdidas o
  inválidas; 404 significa que no hay reserva durable.
- `GET /eventos/reservas/pendientes` y `/eventos/auditoria/stock`: también se
  validan estructura, tipos, cantidades, estados y coherencia entre `total` y
  el número de filas antes de usarlos en recuperación o auditoría.

Eventos ahora devuelve `cantidad` en las respuestas de reserva, confirmación y
liberación. Compras trata JSON vacío/malformado, campos ausentes, tipos
incorrectos, correlaciones distintas y estados inesperados como evidencia no
verificable. En una reserva HTTP 200 inválida, timeout, error de conexión o 5xx,
consulta el estado durable y continúa únicamente si confirma la misma reserva
en estado `reservada`. Si tampoco puede verificarlo, conserva el claim
`processing` y el mismo `operacion_id`, responde 503 con diagnóstico seguro y no
crea compra ni libera stock. Después de persistir la compra, solo marca la
confirmación cuando la respuesta validada o la consulta durable acreditan
`confirmada`; de lo contrario deja el replay pendiente para recovery. Las
confirmaciones/compensaciones también validan su ACK y consultan estado durable
antes de considerar el resultado aplicado.

La compra y su respuesta idempotente se escriben juntas en la transacción local
de `compras_db` después de validar la reserva. La confirmación de Eventos ocurre
después de ese commit local, por lo que sigue siendo una saga entre dos bases,
no una transacción ACID distribuida. Una respuesta durable de Eventos
semánticamente correcta es necesaria para autorizar cada transición; una
respuesta equivocada nunca se convierte por sí sola en venta confirmada.

### Comprobaciones realizadas

- `python3 -m py_compile compras/main.py eventos/main.py tests/test_authentication.py`:
  aprobado.
- `/tmp/nexustix-idempotency-test-venv/bin/python -m pytest -q
  tests/test_authentication.py`: **25 passed** en 5.37 s. Incluye tests aislados
  de contrato, JSON inválido, correlación, timeout después de reserva durable,
  consulta de estado fallida, ACK de acción malformado, compra grupal,
  replay, locks/idempotencia y recuperación. Estos dobles no prueban por sí
  solos la caída real de un servicio.
- Preflight Compose silencioso con proyecto `nexustix-auth-staging`: aprobado;
  los nueve servicios pertenecían a ese proyecto, puertos publicados en
  loopback `18080–18085` y las tres bases estaban saludables. Las revisiones de
  Usuarios/Compras del staging se verificaron en las pruebas HTTP como
  `20261022_01`/`20261023_02`; no se ejecutaron migraciones.
- Se reconstruyeron únicamente las imágenes `nexustix-staging/eventos:auth` y
  `nexustix-staging/compras:auth`. Se actualizó, en secuencia y con `--no-deps`,
  solo `eventos`, `compras1` y `compras2`; no se reiniciaron bases, Usuarios,
  frontend ni balanceador. Todos los seis health checks HTTP de staging
  (`18080–18085`) respondieron 200; las APIs reportaron `db=connected`.
- `NEXUSTIX_STAGING_HTTP=1 ... pytest -q -s
  tests/staging/test_auth_http.py::test_http_purchase_idempotency_with_synthetic_event`:
  **1 passed en 5.73 s**. El evento de fixture se creó directamente en
  PostgreSQL de staging (stock 3); registro/login y compra se hicieron por HTTP
  con JWT. Compra 200, replay 200 con `Idempotency-Replayed: true`, misma clave
  con cuerpo distinto 409. Estado: stock `3→2`, una compra confirmada, un claim,
  una reserva confirmada y un COMMIT de journal, todos con la misma operación.
  La consulta autenticada interna confirmó estado durable `confirmada`. Limpieza
  selectiva verificada para evento, reserva, compra, claim, journal, usuario y
  sesión sintéticos.
- `NEXUSTIX_STAGING_HTTP=1 ... pytest -q -s
  tests/staging/test_auth_http.py::test_concurrent_different_buyers_cannot_oversell_synthetic_staging_event`:
  **1 passed en 3.09 s**. PostgreSQL/HTTP reales: evento sintético con stock 1,
  dos compradores y claves diferentes; respuestas 200/409. Antes: cero
  reservas/compras; después: stock 0, una reserva y compra confirmadas, una
  operación y un COMMIT. El fixture y sus dos cuentas se limpiaron con los
  filtros exactos definidos por la prueba.

Después de la última reconstrucción de Compras, ambas pruebas HTTP sintéticas
se repitieron juntas contra las imágenes finales: **2 passed en 9.40 s**. La
compra/replay conservó HTTP `200/200` y el rechazo de cuerpo distinto `409`; la
carrera de sobreventa volvió a dar `200/409` con una sola venta confirmada. Los
dos fixtures quedaron limpiados y verificados por sus respectivas pruebas.

Las pruebas staging no enviaron datos a los eventos reales ni al entorno
principal. No se probó una caída de proceso, un HTTP 5xx fabricado desde Eventos
ni una respuesta semánticamente falsa del servicio desplegado: esos casos se
cubrieron con dobles unitarios cuando correspondía. No se aplicaron
migraciones ni se afirmó alta disponibilidad integral. La carpeta de trabajo no contiene metadatos Git
accesibles, por lo que no fue posible producir `git diff`; la revisión se hizo
leyendo y compilando los archivos modificados.

### Recovery integrado con PostgreSQL de staging — 2026-10-23

Se adaptó `tests/staging/purchase_recovery_pg_runner.py` a la semántica actual:
para construir un replay pendiente o una reserva sin compra, el test pierde el
ACK remoto **y** hace fallar temporalmente la consulta de estado durable. La
reserva/confirmación original la ejecuta Eventos real y persiste en PostgreSQL;
no se editan directamente claims, compras ni estados de reserva. Si el estado
durable no es verificable, Compras conserva la operación y responde 503 sin
crear una compra ni compensar a ciegas. Una vez retirado el fallo controlado, el
endpoint de recuperación consulta y actúa solo con la evidencia real de ambos
servicios.

Resultado final de
`tests/staging/test_recovery_postgres_http.py::test_recovery_reconciles_real_postgres_and_events_without_duplicate_effects`:
**1 passed en 130.03 s**; PostgreSQL `15.19` en las tres bases, Compras Alembic
`20261023_02`, Usuarios `20261022_01`, umbral stale 120 s y espera real 122 s.
Las peticiones de recuperación fueron HTTP 200 en ambos barridos. El status
global `recuperacion_incompleta` es intencional: el claim sin reserva permanece
reintentable y diagnosticado, no representa una falla del test.

| Escenario | Estado previo a recovery | Después del primer recovery | Segunda pasada y retry |
|---|---|---|---|
| Compra persistida/reserva confirmada, replay `pendiente` | Compra 1, claim `completed`, reserva `confirmada`, stock 1 (inició en 2); el request sintético recibió HTTP 200. | Replay corregido a `confirmada`; contador `replays_actualizados=1`; no se crea otra compra ni cambia stock. | Sin cambios, `replays_actualizados=0`. |
| Claim antiguo sin reserva | Compra 0, claim `processing`, reserva 404, stock 2; request HTTP 503. | Diagnóstico `claim_sin_reserva_reintentable`; sin compra ni cambio de stock. | Mismo diagnóstico, sin cambios; retry de la misma clave HTTP 200 crea una sola compra/reserva confirmada, usando el mismo `operacion_id`, stock 2→1. |
| Reserva antigua sin compra | Compra 0, claim `processing`, reserva `reservada`, stock 1 (inició en 2); request HTTP 503 mientras el estado durable no era verificable. | Tras verificar estado exacto y antigüedad, se libera una vez; claim termina con HTTP replay 409, reserva `liberada`, stock vuelve a 2, cero compras. | No repite liberación ni modifica el estado; `reservas_liberadas=0`. |

La primera pasada contó una reserva confirmada ya coherente, actualizó un replay,
diagnosticó un claim sin reserva y liberó una reserva sin compra cuyo estado
durable era exacto. No dejó operaciones ambiguas. El test confirmó los
snapshots de ambas bases y la igualdad de los snapshots antes/después de la
segunda pasada. Limpieza exacta verificada: 3 eventos, 3 reservas, 2 compras,
3 claims y 5 filas de journal. La sesión refresh del administrador QA fue
revocada; su fila de auditoría no se borró.

Dos intentos iniciales del test reutilizaron expectativas antiguas: bajo la
nueva implementación, perder solo el ACK permite verificar el estado durable y
continuar correctamente. Ambos dejaron fixtures de staging para inspección;
las consultas mostraron compra/reserva coherentes o claims recuperables. Se
limpiaron únicamente esos fixtures mediante transacciones con guardas exactas,
y los conteos posteriores quedaron en cero antes de ejecutar la prueba final.
No se alteraron eventos QA existentes ni el entorno principal.

La recuperación se probó con servicios Eventos y PostgreSQL reales, pero la
compra inicial del helper invoca la función de Compras dentro del contenedor,
no el endpoint HTTP público de compra. Se simularon pérdidas controladas de
respuesta/consulta; no fue una caída abrupta de proceso. Aún no se validaron
fallos reales de PostgreSQL, HTTP 5xx desde Eventos ni caídas abruptas.

## 12. Actualización de Entrega y Cierre Técnico — 2026-10-24

### Acciones Realizadas en esta Iteración
1. **Migración a Lifespan:**
   - `eventos/main.py` y `usuarios/main.py` migrados de `@app.on_event("startup")` al gestor asíncrono `@asynccontextmanager async def _lifespan(app)`.
   - `compras/main.py` conserva `@app.on_event("startup")` intencionalmente para no desestabilizar la inicialización del `MiddlewareMetricas` y dependencias cruzadas de 1898 líneas.
2. **Nuevas Pruebas Unitarias Aisladas:**
   - Añadidas pruebas en `tests/test_authentication.py`:
     - `test_compras_simulation_shutdown_health_and_failover_status`: valida que al simular apagado (`SIMULACION_APAGADO=True`), `/health` retorna HTTP 503 con `status=down`, y `validar_nodo_disponible()` lanza excepción HTTP 503 con detalle `apagado_simulado` para activar failover pasivo en Nginx.
     - `test_compras_simulation_routes_require_admin`: verifica que los endpoints de control y simulación requieren rol administrador y rechazan a compradores con HTTP 403.
   - Resultado de la suite unitaria completa: **42 passed en 6.73 s** (`tests/test_authentication.py`, `tests/test_backup_procedure.py`, `tests/test_locust_safety.py`).
3. **Plan de Pruebas de 18 Etapas:**
   - Documento `docs/PLAN_PRUEBAS_FALLOS.md` estructurado y completado con las 18 etapas ordenadas por riesgo, con preparación, comandos de staging, resultados esperados, criterios de aprobación y procedimientos de recuperación.
4. **Diseño de Dos PC:**
   - `docs/ARQUITECTURA_DOS_PC.md` actualizado con topología de red, matriz de puertos, aislamiento de claves privadas, análisis de caída de PC-A y PC-B, prevención de split-brain y modelos de Compose conceptuales.
5. **Diagnóstico Determinista para Compras2:**
   - Creado `scripts/diagnostico_compras2_crash.sh` para registrar Container ID, RestartCount, ExitCode, políticas Docker y logs antes de repetir pruebas destructivas.
6. **Revisión y Correcciones Críticas Adicionales:**
   - **Causa del código 2:** La ejecución de `ls -l scripts/diagnostico_compras2_crash.sh` retornó código 2 porque el subagente previo colapsó por cuota antes de crear el archivo. El script fue posteriormente creado y verificado.
   - **Corrección en `eventos/main.py`:** Se detectó que el startup anterior había sido omitido durante la primera edición de lifespan. Se restauró el gestor asíncrono `_lifespan` ejecutando `esperar_bd()` y la siembra de `EVENTOS_BASE` en arranque.
   - **Corrección en `scripts/backup_main_databases.py`:** Se corrigió la función `_run_migration_rehearsal`, que exigía `20261022_01` para todos los servicios, impidiendo ensayar migraciones sobre `compras_db` cuya revisión actual es `20261023_02`.
   - **Precisión en Arquitectura Dos PC:** Se corrigió la calificación de "tolerancia completa" ante la caída de PC-B a "disponibilidad parcial degradada", documentando que los clientes dirigidos a PC-B se desconectan de inmediato (sin VIP), Nginx en PC-A sufre penalización de latencia por timeout y las reservas en vuelo requieren reconciliación manual.


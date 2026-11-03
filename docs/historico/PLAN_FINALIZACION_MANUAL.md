# Continuidad manual y cierre técnico de NEXUSTIX

**Actualizado:** 2026-10-24. Esta guía describe estado observado/revisado en el
workspace. No es autorización para migrar, reiniciar servicios o escribir en el
entorno principal. Cualquier prueba que modifique staging se identifica aparte.

## Estado

- Auth: registro público comprador, Argon2id, JWT RS256 corto, refresh rotatorio
  en cookie HttpOnly, autorización backend; Compras autentica JWT localmente.
- Compra individual: claim/idempotency compartido entre las réplicas,
  request hash, replay, estado durable `operacion_id`, reservas de Eventos.
- Events contract: respuesta remota se valida por código, JSON/tipos, IDs,
  cantidad/estado; una respuesta dudosa queda recuperable, no se compensa a
  ciegas.
- Recovery `/compras/recuperar-servidor`: administrativo y manual; no existe
  worker periódico. El journal es best-effort.
- Failover Compras ordenado: ambas réplicas demostradas en staging. Crash
  abrupto Compras1 tiene cuatro escenarios aprobados históricos; Compras2 sigue
  inconcluso y no se repite hasta corregir la observabilidad del test.
- Las tres DB, Usuarios, Eventos, frontend, balanceador y host siguen siendo
  SPOF. No se ha desplegado dos-PC.
- Backup: existe `scripts/backup_main_databases.py`; un backup histórico de
  2026-10-23 se documentó como verified. No se revalidó aquí. No hay PITR ni
  replicación de DB.
- Defecto corregido en esta entrega: health ya no devuelve excepciones SQL y
  usa HTTP 503 si falla la consulta DB. El perfil Locust previo fue reemplazado
  por uno autenticado, idempotente y limitado a staging loopback.

## Archivos clave

- API: `usuarios/main.py`, `eventos/main.py`, `compras/main.py`.
- Persistencia: `*/models.py`, `*/database.py`, `*/migrations/versions/`.
- Compose: `docker-compose.yml` principal, `docker-compose.staging.yml` aislado.
- Balanceador: `compras_lb/nginx.conf`.
- Staging HTTP: `tests/staging/test_auth_http.py`,
  `test_purchase_failover_http.py`, `test_purchase_abrupt_crash_http.py`,
  `test_events_unavailability_http.py`, `test_recovery_postgres_http.py`.
- Backup: `scripts/backup_main_databases.py`, `docs/RESPALDO_RESTAURACION.md`.
- Planes: `MATRIZ_TOLERANCIA_FALLOS.md`, `PLAN_BACKUP_RESTAURACION.md`,
  `ARQUITECTURA_DOS_PC.md`, `PLAN_PRUEBAS_FALLOS.md`.

## 1. Validaciones estáticas locales

Desde Fedora, sin activar servicios:

```bash
cd "$HOME/Documents/Informatica/VIII CICLO/2.Sistemas operativos 2/Unit 2/Project"
python3 -m venv "$HOME/.venvs/nexustix-test"
source "$HOME/.venvs/nexustix-test/bin/activate"
python -m pip install -r requirements-test.txt
python -m compileall -q usuarios compras eventos shared scripts tests locustfile.py
python -m pytest -q tests/test_authentication.py tests/test_backup_procedure.py tests/test_locust_safety.py
node --test tests/staging/frontend_auth_state.test.js
```

Esperado: compilación sin errores; todas las pruebas unitarias listadas pasan.
Esto no valida PostgreSQL concurrente, contenedores, red ni HTTP real. No
ejecutar `tests/staging` sin los flags porque puede escribir staging.

## 2. Preflight del entorno principal (solo configuración, no arrancar)

No mostrar Compose resuelto ni variables secretas. Confirma solo éxito/fallo:

```bash
docker compose --env-file "$HOME/.config/nexustix-main.env" \
  --project-name project -f docker-compose.yml config --quiet
docker compose --env-file "$HOME/.config/nexustix-main.env" \
  --project-name project -f docker-compose.yml ps --all
```

`config --quiet` valida referencias; `ps` consulta estado. No arranca, detiene,
reconstruye ni cambia DB. Si el proyecto/nombres no son los previstos, parar.
No ejecutar Alembic, bootstrap ni `up` sin aprobación de despliegue y backup.

## 3. Preflight/arranque staging

La siguiente validación configura explícitamente staging y no imprime secretos:

```bash
export NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets"
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml config --quiet
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml ps --all
```

**Modifica staging** si se usa `up`; no afecta el Compose principal. Antes de
hacerlo, verificar proyecto, nombres, puertos loopback, volúmenes y secretos.
Para iniciar solo servicios faltantes sin recrear los existentes:

```bash
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml \
  up -d --no-recreate --wait
```

Si Compose informa cambios o `no-recreate` no puede satisfacer la config,
detenerse y revisar. Health de solo lectura:

```bash
for port in 18081 18082 18083 18084 18085; do
  curl --silent --show-error --output /dev/null --write-out "%{http_code} $port\n" "http://127.0.0.1:$port/health"
done
curl --silent --show-error --output /dev/null --write-out "%{http_code} 18080\n" http://127.0.0.1:18080/
```

Esperado: 200 y DB `connected` para APIs. Esta comprobación es superficial; no
verifica escritura, autorización o recuperación.

## 4. Compra normal e idempotencia (escribe únicamente en staging)

Con staging identificado y fixtures QA autorizados, usar tests que registran
compradores sintéticos, usan JWT, generan `Idempotency-Key` único y hacen
limpieza selectiva:

```bash
NEXUSTIX_STAGING_HTTP=1 python -m pytest -q -s \
  tests/staging/test_auth_http.py::test_registration_login_and_authentication_required \
  tests/staging/test_auth_http.py::test_buyer_purchase_is_owned_and_history_is_private
NEXUSTIX_STAGING_HTTP=1 python -m pytest -q -s \
  tests/staging/test_auth_http.py::test_purchase_idempotency_replays_and_rejects_changed_request \
  tests/staging/test_auth_http.py::test_concurrent_purchase_retries_create_one_order \
  tests/staging/test_auth_http.py::test_shared_postgres_idempotency_across_both_purchase_replicas
```

Resultado esperado: una compra para misma key/subject/body, replay sin débito,
409 al cambiar cuerpo/sujeto; confirmar operación, claim, reserva y stock. Si el
test no muestra `passed` o limpieza exacta, no considerarlo aprobado.

## 5. Concurrencia y sobreventa

```bash
NEXUSTIX_STAGING_HTTP=1 python -m pytest -q -s \
  tests/staging/test_auth_http.py::test_concurrent_different_buyers_cannot_oversell_synthetic_staging_event
```

Modifica temporalmente staging con evento de stock 1 y dos cuentas QA. Esperado:
un éxito, un rechazo 409, una reserva/compra confirmada y stock cero; fixture
limpia solo sus IDs. Abortar y conservar evidencia ante limpieza incompleta.

## 6. Recuperación

```bash
NEXUSTIX_STAGING_HTTP=1 python -m pytest -q -s \
  tests/staging/test_recovery_postgres_http.py::test_recovery_reconciles_real_postgres_and_events_without_duplicate_effects
```

Escribe una operación sintética en PostgreSQL y Eventos staging. Revisar snapshot
antes/después y segunda ejecución idempotente. No ejecutar el endpoint de
recovery contra principal. Ante una operación externa ambigua, conservar datos.

## 7. Failover ordenado

Solo en ventana de staging, el test de un caso detiene una réplica, la restaura
en `finally`, verifica datos y health. Es una parada ordenada, no un crash:

```bash
NEXUSTIX_STAGING_HTTP=1 python -m pytest -q -s \
  tests/staging/test_purchase_failover_http.py::test_http_purchase_continues_when_compras1_stopped
NEXUSTIX_STAGING_HTTP=1 python -m pytest -q -s \
  tests/staging/test_purchase_failover_http.py::test_http_purchase_continues_when_compras2_stopped
```

No ejecutar ambos en paralelo. Esperar nueve servicios activos y tres DB
healthy al final.

## 8. Caída abrupta

Compras1 ha tenido cuatro checkpoints abruptos aprobados históricamente. La
prueba reemplaza contenedores en `finally`; capturar evidencia antes de esa fase.
Compras2 queda **pendiente/inconclusa**. Se ha creado el script:
`bash scripts/diagnostico_compras2_crash.sh`
para capturar deterministamente el Container ID, RestartCount, ExitCode,
política de reinicio y últimos logs de `compras2` sin depender de la recreación
en `finally`. No ejecutar `test_purchase_abrupt_crash_http.py` para Compras2
hasta haber ejecutado dicho diagnóstico y verificar la causa del estado `running`.

No parar Eventos, Usuarios, DB, Nginx ni ambas réplicas a la vez. Una parada
ordenada no demuestra terminación abrupta. Una caída de proceso tampoco simula
pérdida de host o DB.

## 9. Locust (solo staging y fixture sintético)

Crear un comprador sintético QA por flujo HTTP y un evento sintético de stock
controlado antes de usar la carga. No apuntar a IDs reales/seeds. El script se
niega a correr si el target no es loopback `18085`, Usuarios no es loopback
`18081` o falta opt-in. Instala Locust en el venv separado, no en contenedor:

```bash
python -m pip install 'locust>=2.31,<3'
export NEXUSTIX_STAGING_HTTP=1
export NEXUSTIX_LOCUST_USERS_URL=http://127.0.0.1:18081
export NEXUSTIX_LOCUST_EMAIL='comprador-sintetico@example.test'
read -r -s -p 'Contraseña sintética: ' NEXUSTIX_LOCUST_PASSWORD; echo
export NEXUSTIX_LOCUST_PASSWORD
export NEXUSTIX_LOCUST_EVENT_ID='ID_DE_EVENTO_SINTETICO'
locust -f locustfile.py --host http://127.0.0.1:18085 --headless \
  --users 2 --spawn-rate 1 --run-time 30s --stop-timeout 5
unset NEXUSTIX_LOCUST_PASSWORD
```

Modifica solo staging. Se crea una compra lógica nueva por tarea; 409 se muestra
como rechazo de inventario agotado, no como fallo de infraestructura. No se ha
ejecutado esta carga en la entrega. Para un perfil moderado, primero revisar la
prueba pequeña y luego elevar gradualmente a 10 usuarios/2 por segundo por 2
minutos, con autorización de ventana. Abortar en 5xx, stock negativo,
duplicación, latency creciente o estado ambiguo. Revisar claims/compras/reservas
por prefijo/usuario sintético y reconciliar antes de limpiar.

## 10. Backup, restore y limpieza

> ⚠️ **Advertencia de Aislamiento y Riesgos del Script de Respaldo:**
> - `scripts/backup_main_databases.py` inspecciona contenedores con nombres fijos
>   `usuarios_db`, `eventos_db` y `compras_db`, que corresponden exclusivamente a las
>   etiquetas del proyecto Compose principal (`--compose-project project`).
> - Si se pasa un proyecto diferente (como `nexustix-auth-staging`), la validación
>   de etiquetas de Compose (`_docker_label`) **rechaza la ejecución** para evitar
>   mezclar entornos, o falla si el contenedor principal no existe.
> - **Corrección crítica aplicada:** Se corrigió el ensayo de migraciones
>   (`--rehearse-migrations`), que anteriormente exigía de forma rígida la revisión
>   `20261022_01` para todos los servicios, provocando fallo al verificar `compras_db`
>   cuya revisión head vigente es `20261023_02`.
> - La captura de backup lee directamente las bases de datos; ejecutarla únicamente
>   en una ventana explícitamente autorizada tras pausar escrituras.

```bash
umask 077
BACKUP_DIR="$HOME/nexustix-backups/$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/backup_main_databases.py backup --output-dir "$BACKUP_DIR" \
  --compose-project project --confirm-writes-paused
python3 scripts/backup_main_databases.py verify --backup-dir "$BACKUP_DIR" \
  --rehearse-migrations
```

`backup` lee datos del principal. `verify` solo usa dumps y crea Postgres/red
aislados. No ejecutar backup nuevo hasta autorización de lectura. Nunca restaurar
en principal durante esta tarea. No usar `scripts/reset_entorno_pruebas.sh`,
`docker compose down -v`, `docker system prune` ni eliminación de volúmenes.
Verificar limpieza con el resultado explícito de cada test; si no coincide,
parar borrados y guardar IDs/operación sintéticos para revisión.

## Lista de cierre

- **Completada:** auth/JWT, idempotencia individual, reservas y recovery
  conservador implementados; resultados históricos en auditoría.
- **Completada en esta entrega:** health SQL responde 503 sin detalle SQL;
  migración a lifespan en eventos y usuarios; 42 pruebas unitarias pasando.
- **Completada en esta entrega:** Script de diagnóstico determinista para Compras2
  (`scripts/diagnostico_compras2_crash.sh`) e instrumentación de observabilidad.
- **Completada en esta entrega:** Locust ya envía JWT/key y acepta solo URLs
  loopback staging.
- **Pendiente validación local:** ejecutar suite unitaria/estática después de
  los cambios de esta entrega.
- **Pendiente manual:** pruebas staging después de autorizar ejecución; caída de
  Usuarios y DB; restore de una copia vigente; Locust con evento sintético.
- **Pendiente crítica:** terminación abrupta Compras2; no repetir hasta
  instrumentación determinista.
- **Pendiente de infraestructura:** backup off-host automatizado, PITR/replica,
  fencing/quórum y despliegue dos-PC.
- **No requerido ahora:** Kubernetes/Kafka/Redis u otros componentes no
  justificados por los recursos actuales.

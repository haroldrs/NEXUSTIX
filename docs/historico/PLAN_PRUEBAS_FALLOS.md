# Plan escalonado de pruebas de fallos NEXUSTIX

No se ejecutaron pruebas de fallos globales en esta misión. Todas las pruebas
que escriben, detienen servicios o restauran datos son manuales y solo se
autorizan después de preflight inequívoco. Usar el proyecto
`nexustix-auth-staging`, archivo `docker-compose.staging.yml`, puertos
loopback `18080–18085`, secretos staging. Abortar si IDs/nombres, puertos,
volúmenes, revisiones, hooks o DB no coinciden. Nunca usar `down -v`, scripts de
reset, ni ejecutar sobre `docker-compose.yml` principal.

---

## Estado general al 2026-10-24

| Categoría | Detalle |
|---|---|
| Staging activo | 9 servicios en `nexustix-auth-staging` (puertos 18080–18085) |
| Tests unitarios | 40 passed |
| Migración lifespan | `eventos/main.py` y `usuarios/main.py` migrados a `lifespan`; `compras` mantiene `on_event` por complejidad |
| Pruebas aprobadas | Failover ordenado (ambas réplicas), crash abrupto Compras1 (4 checkpoints), indisponibilidad Eventos, recovery con PostgreSQL real, idempotencia, sobreventa concurrente |
| Pendientes | Crash abrupto Compras2 (inconcluso: problema de observabilidad del contenedor), pruebas de DB caída, prueba de carga Locust |
| SPOF documentados | Usuarios, Eventos, las 3 bases, Nginx, host |

---

## Preflight y evidencia mínima staging

Comandos de solo lectura (no muestran el Compose resuelto ni secretos):

```bash
export NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets"
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml config --quiet
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml ps --all
```

Antes de cualquier escritura, verificar exclusivamente los seis puertos locales
de staging, los nueve servicios (3 DB + 6 aplicaciones/edge), nombres de
contenedor/proyecto y revisiones requeridas. Guardar una línea de base sintética
por `evento_id`, key, subject y `operacion_id`. No mostrar cookies/tokens.

Por escenario: hora UTC, request URL/instancia, status/latencia, container ID,
State/ExitCode/RestartCount/StartedAt, snapshots antes/después de stock, claim,
compra, reserva, journal y resultado de replay. Limpiar solo con filtros exactos
del fixture; si algo difiere, conservar datos para investigación.

Comandos de ejecución ya preparados se encuentran en
`PLAN_FINALIZACION_MANUAL.md`. La prueba de carga no sustituye pruebas de
integridad; una respuesta 409 por stock agotado es rechazo de negocio que debe
separarse de errores de infraestructura.

---

## Tabla de 18 etapas

### Etapa 1 — Arranque y salud de todos los servicios

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que los 9 servicios staging arrancan correctamente y pasan health-check |
| **Preparación** | Preflight completo; verificar puertos 18080–18085; no debe haber contenedores de otras ejecuciones |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml ps --all` <br>`docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml logs --tail=30` |
| **Resultado esperado** | 9 servicios en estado `running`/`healthy`; 0 reinicios recientes |
| **Datos a verificar** | `State`, `Status`, `RestartCount`, `StartedAt` de cada contenedor; health HTTP 200 en `/health` de cada API |
| **Criterio de aprobación** | Todos los servicios `healthy`, sin errores en logs de arranque, puertos correctos |
| **Recuperación** | Si un servicio no arranca, revisar logs sin reiniciar; no ejecutar etapas siguientes hasta resolver |
| **Estado** | ✅ **Probado** — suite auth staging e idempotencia reportadas pasadas; revalidación confirmada en staging activo (2026-10-24) |

---

### Etapa 2 — Registro e inicio de sesión

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar flujo completo de registro de usuario y autenticación JWT |
| **Preparación** | Staging saludable; fixtures únicos por ejecución (evitar colisiones de email/username) |
| **Comandos (staging)** | `curl -s -X POST http://localhost:18080/usuarios/registro -H 'Content-Type: application/json' -d '{"email":"test_TIMESTAMP@nexus.test","password":"Test1234!","nombre":"Test"}'` <br>`curl -s -X POST http://localhost:18080/usuarios/login -H 'Content-Type: application/json' -d '{"email":"test_TIMESTAMP@nexus.test","password":"Test1234!"}'` |
| **Resultado esperado** | Registro: 201; Login: 200 con `access_token` válido |
| **Datos a verificar** | JWT decodificable; `exp` dentro del rango esperado; `sub` corresponde al usuario registrado |
| **Criterio de aprobación** | Ambos endpoints responden correctamente; token usado en compra posterior produce 200 |
| **Recuperación** | Si falla registro, no continuar; limpiar fixture con filtro exacto de email |
| **Estado** | ✅ **Probado** — suite de autenticación HTTP staging aprobada históricamente |

---

### Etapa 3 — Compra normal

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que el flujo completo de compra funciona en condiciones normales |
| **Preparación** | Usuario autenticado (Etapa 2); evento con stock > 0; `operacion_id` UUID único |
| **Comandos (staging)** | `curl -s -X POST http://localhost:18080/compras/ -H "Authorization: Bearer TOKEN" -H 'Content-Type: application/json' -d '{"evento_id":1,"cantidad":1,"operacion_id":"UUID"}'` <br>Verificar stock: `curl -s http://localhost:18080/eventos/1` |
| **Resultado esperado** | 200/201; `stock_disponible` decrementado; reserva persistida en Eventos; compra en Compras |
| **Datos a verificar** | `stock_disponible` antes/después; registro en `compras_db`; reserva en `eventos_db`; journal |
| **Criterio de aprobación** | `stock = stock_inicial - cantidad`; compra y reserva coherentes; no hay doble descuento |
| **Recuperación** | Si el stock no decrementó, no repetir sin identificar causa; conservar `operacion_id` para replay |
| **Estado** | ✅ **Probado** — compra normal verificada en pruebas de idempotencia y failover |

---

### Etapa 4 — Idempotencia

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que reenviar la misma `operacion_id` no genera duplicados ni descuenta stock adicional |
| **Preparación** | Compra exitosa con `operacion_id` conocido (Etapa 3) |
| **Comandos (staging)** | Repetir el mismo request de Etapa 3 con el mismo `operacion_id` al menos 2 veces más |
| **Resultado esperado** | Segunda y tercera solicitud retornan la misma respuesta original (200/201 o 409 idempotente); stock no varía |
| **Datos a verificar** | Stock antes y después de cada replay; registros en `compras_db` (no deben duplicarse); respuesta idéntica |
| **Criterio de aprobación** | Exactamente 1 compra registrada; stock igual al de Etapa 3; sin errores 5xx |
| **Recuperación** | Si hay duplicados, preservar datos; no limpiar hasta identificar la fuente del error |
| **Estado** | ✅ **Probado** — idempotencia aprobada históricamente en staging |

---

### Etapa 5 — Concurrencia y sobreventa

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que bajo carga concurrente no se vende más stock del disponible |
| **Preparación** | Evento con stock bajo (ej. 2); N requests simultáneos con `operacion_id` distintos (N > stock) |
| **Comandos (staging)** | Lanzar requests paralelos con `xargs` o script Python; registrar todos los `operacion_id` usados |
| **Resultado esperado** | Exactamente `stock_inicial` compras exitosas; el resto retorna 409 (stock agotado) |
| **Datos a verificar** | Count de 200 == stock_inicial; count de 409 == N - stock_inicial; stock final == 0; sin 5xx inesperados |
| **Criterio de aprobación** | Ninguna sobreventa; coherencia entre `compras_db` y `eventos_db`; no hay bloqueos indefinidos |
| **Recuperación** | Si hay sobreventa, parar aceptación de compras y auditar; no destruir datos |
| **Estado** | ✅ **Probado** — sobreventa concurrente aprobada históricamente en staging |

---

### Etapa 6 — Caída ordenada de Compras1

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que al detener Compras1 ordenadamente, Compras2 asume el tráfico sin pérdida |
| **Preparación** | Preflight; capturar container ID y RestartCount de compras1; tener `operacion_id` nuevo listo |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop compras1` <br>Probar compra vía Nginx (puerto 18080); `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml start compras1` |
| **Resultado esperado** | Durante caída: compra exitosa por compras2; al restaurar: ambas saludables |
| **Datos a verificar** | Que Nginx enruta a compras2 durante la pausa; stock correcto; reserva en Eventos |
| **Criterio de aprobación** | 0 errores 5xx durante la ventana de parada; recovery sin datos perdidos |
| **Recuperación** | `start compras1`; verificar estado antes de continuar |
| **Estado** | ✅ **Probado** — caída ordenada de ambas réplicas aprobada históricamente |

---

### Etapa 7 — Caída ordenada de Compras2

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que al detener Compras2 ordenadamente, Compras1 asume el tráfico sin pérdida |
| **Preparación** | Igual que Etapa 6 pero con compras2 |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop compras2` <br>Probar compra; `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml start compras2` |
| **Resultado esperado** | Compra exitosa por compras1 durante pausa; recovery limpio |
| **Datos a verificar** | Stock, reserva, claims; Nginx enruta a compras1; ambas saludables al restaurar |
| **Criterio de aprobación** | 0 errores 5xx; coherencia de datos |
| **Recuperación** | `start compras2`; verificar estado |
| **Estado** | ✅ **Probado** — caída ordenada de ambas réplicas aprobada históricamente |

---

### Etapa 8 — Caída abrupta de Compras1

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar recovery tras `kill -9` del proceso de Compras1 (4 checkpoints) |
| **Preparación** | Capturar container ID, RestartCount, State, StartedAt, policy de restart **antes** del evento |
| **Comandos (staging)** | Usar hook staging de crash: `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml exec compras1 kill -9 1` <br>Capturar `docker events` y `docker inspect` inmediatamente; ejecutar compra de prueba |
| **Resultado esperado** | Compras2 absorbe tráfico; compras1 reinicia según política; estado post-crash coherente |
| **Datos a verificar** | ExitCode (137), nuevo container ID o mismo con RestartCount+1, StartedAt actualizado, recovery idempotente |
| **Criterio de aprobación** | Los 4 checkpoints documentados aprobados: exit code correcto, política efectiva, ID correcto, recovery idempotente |
| **Recuperación** | No repetir sin verificar terminación determinista; restaurar si no hay auto-restart |
| **Estado** | ✅ **Probado** — cuatro escenarios de crash abrupto Compras1 aprobados históricamente |

---

### Etapa 9 — Caída abrupta de Compras2

> ⚠️ **PENDIENTE — No ejecutar hasta instrumentar observabilidad del contenedor**

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar recovery tras `kill -9` del proceso de Compras2 (análogo a Etapa 8) |
| **Preparación** | **Primero ejecutar `scripts/diagnostico_compras2_crash.sh`** para verificar que el container ID, RestartCount, ExitCode y event stream son observables antes del evento. Sin esa baseline, el test es inconcluso. |
| **Comandos (staging)** | Ejecutar diagnóstico previo; luego: `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml exec compras2 kill -9 1` <br>Capturar `docker events` y `docker inspect` inmediatamente |
| **Resultado esperado** | Compras1 absorbe tráfico; compras2 reinicia; recovery idempotente; ExitCode 137 |
| **Datos a verificar** | ExitCode, RestartCount, StartedAt, container ID, logs post-crash, coherencia de stock/reserva |
| **Criterio de aprobación** | Mismos 4 checkpoints que Etapa 8 pero para compras2; diagnóstico previo exitoso |
| **Recuperación** | Restaurar compras2 si no hay auto-restart; preservar logs |
| **Estado** | ⏳ **PENDIENTE** — inconcluso por problema de observabilidad del contenedor; requiere diagnóstico con script dedicado antes de reejecutar |

---

### Etapa 10 — Caída de Usuarios (API y DB)

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar comportamiento del sistema cuando el servicio Usuarios no está disponible |
| **Preparación** | Usuario con JWT válido ya obtenido; evento con stock; `operacion_id` nuevo |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop usuarios` <br>Intentar login; intentar compra con JWT existente; `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml start usuarios` |
| **Resultado esperado** | Login falla (503/502); compras con JWT ya emitido pueden o no funcionar según si Compras verifica contra Usuarios; no hay JWT inválido silencioso |
| **Datos a verificar** | Código HTTP de login, comportamiento de Compras con JWT existente, comportamiento al restaurar Usuarios |
| **Criterio de aprobación** | Errores claros (no 200 falso); sin datos corrompidos; recuperación limpia al restaurar |
| **Recuperación** | `start usuarios`; verificar health; no emitir compras en estado ambiguo |
| **Estado** | ❌ **No probado en staging** — solo pruebas unitarias; pendiente ejecución manual |

---

### Etapa 11 — Caída de Eventos (API y DB por separado)

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar comportamiento al caer el servicio Eventos y al caer solo su base de datos |
| **Preparación** | Fixtures únicos; stock disponible; escenario A: parar API Eventos; escenario B: parar solo `eventos_db` |
| **Comandos (staging)** | **Escenario A:** `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop eventos` <br>Intentar compra → debe fallar antes de reservar; restaurar: `start eventos` <br>**Escenario B:** `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop eventos_db` <br>Intentar compra → Eventos falla al contactar DB; restaurar: `start eventos_db` |
| **Resultado esperado** | Ninguna compra sin reserva confirmada; `operacion_id` durable; no liberación a ciegas de stock |
| **Datos a verificar** | Que no haya compra sin reserva; que la respuesta sea error claro; que tras restaurar el stock sea coherente |
| **Criterio de aprobación** | Sin compras huérfanas; sin stock decrementado sin reserva; recovery limpio |
| **Recuperación** | No lanzar más compras hasta restaurar; reconciliar por `operacion_id` si hay ambigüedad |
| **Estado** | ⚠️ **Parcialmente documentado** — escenarios anotados históricamente; ejecución en staging pendiente; caída de `eventos_db` aislada no probada |

---

### Etapa 12 — Caída del balanceador Nginx

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar comportamiento cuando el balanceador cae; verificar que APIs directas siguen operando |
| **Preparación** | Conocer puertos directos de compras1 (18081) y compras2 (18082); tener JWT válido |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop nginx` <br>Intentar GET `http://localhost:18080/` → debe fallar; intentar compra directa en `http://localhost:18081/compras/`; restaurar: `start nginx` |
| **Resultado esperado** | Nginx caído: acceso por 18080 falla; acceso directo a APIs funciona; al restaurar Nginx: todo vuelve |
| **Datos a verificar** | Código de error con Nginx caído; que compra directa funcione; que no haya pérdida de datos |
| **Criterio de aprobación** | Fallo explícito de 18080; APIs directas operativas; recovery limpio de Nginx |
| **Recuperación** | `start nginx`; verificar health de todos los servicios |
| **Estado** | ❌ **No probado** — pendiente ejecución manual en staging |

---

### Etapa 13 — Indisponibilidad de `usuarios_db`

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar comportamiento de Usuarios cuando su DB no está disponible |
| **Preparación** | JWT válido ya emitido y disponible; no hacer login durante la prueba |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop usuarios_db` <br>GET `http://localhost:18080/usuarios/health` → esperar 503; intentar login; intentar operación con JWT existente en Compras; restaurar: `start usuarios_db` |
| **Resultado esperado** | Health de Usuarios retorna 503/unhealthy; login falla; operaciones con JWT existente según implementación |
| **Datos a verificar** | Health HTTP, respuesta de login, comportamiento de Compras con JWT preexistente, recovery al restaurar DB |
| **Criterio de aprobación** | Errores claros y sin información sensible; recovery limpio; sin datos corrompidos |
| **Recuperación** | `start usuarios_db`; esperar que Usuarios vuelva a healthy; no emitir JWT durante la ventana |
| **Estado** | ❌ **No probado en staging** — solo unitarias; pendiente ejecución manual |

---

### Etapa 14 — Indisponibilidad de `eventos_db`

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar comportamiento de Eventos cuando su DB no está disponible |
| **Preparación** | Evento con stock conocido; `operacion_id` nuevo; no lanzar compras durante la ventana |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop eventos_db` <br>GET `http://localhost:18080/eventos/health` → esperar 503; intentar compra → debe fallar sin reserva; restaurar: `start eventos_db` |
| **Resultado esperado** | Eventos unhealthy; Compras no puede reservar → rechaza compra con error claro; al restaurar: coherencia de stock |
| **Datos a verificar** | Health de Eventos, que no haya compra registrada sin reserva, stock intacto durante la ventana |
| **Criterio de aprobación** | Sin compras huérfanas; sin decremento de stock sin reserva; recovery limpio |
| **Recuperación** | `start eventos_db`; reconciliar por `operacion_id` si hubo ambigüedad |
| **Estado** | ❌ **No probado en staging** — solo unitarias; pendiente ejecución manual |

---

### Etapa 15 — Indisponibilidad de `compras_db`

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar comportamiento de Compras cuando su DB no está disponible |
| **Preparación** | Eventos y Usuarios saludables; no lanzar compras durante la ventana |
| **Comandos (staging)** | `docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop compras_db` <br>GET `http://localhost:18080/compras/health` → esperar 503; intentar compra → debe fallar; restaurar: `start compras_db` |
| **Resultado esperado** | Compras unhealthy; compra falla antes de tocar Eventos; stock y reservas intactos; recovery limpio |
| **Datos a verificar** | Health de ambas réplicas Compras, que no haya reserva creada sin compra registrada, recovery limpio |
| **Criterio de aprobación** | Sin reservas huérfanas; sin stock decrementado sin compra registrada; recovery sin intervención manual |
| **Recuperación** | `start compras_db`; verificar health de compras1 y compras2 antes de reanudar |
| **Estado** | ❌ **No probado en staging** — solo unitarias; pendiente ejecución manual |

---

### Etapa 16 — Backup y restauración aislada

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que el backup es íntegro y restaurable en un entorno aislado |
| **Preparación** | Backup reciente disponible; entorno de restauración aislado (red interna / tmpfs); no restaurar sobre servicios activos |
| **Comandos (staging)** | `python3 scripts/backup_main_databases.py --verify` (modo verify sobre backup existente) <br>Restauración en entorno temporal aislado; verificar hashes, conteos, stock, reservas, compras, claims, journal |
| **Resultado esperado** | Hashes/conteos/stock/reservas/compras/claims/journal y ensayo Alembic coinciden con original |
| **Datos a verificar** | Hash del backup, conteos de tablas clave, coherencia de stock vs reservas, resultado de Alembic en modo dry-run |
| **Criterio de aprobación** | Backup `verified`; restauración completa sin errores; datos coherentes |
| **Recuperación** | Si falla, preservar fixture y logs; no borrar para reintentar; investigar antes de repetir |
| **Estado** | ✅ **Verificado históricamente** — backup `verified` reportado; no revalidado en turno actual |

---

### Etapa 17 — Recuperación y reconciliación (recovery endpoint)

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar que el endpoint de recovery concilia operaciones tras fallo parcial |
| **Preparación** | Operación con `operacion_id` conocido que quedó en estado ambiguo (simulado); Compras y Eventos saludables |
| **Comandos (staging)** | `curl -s -X POST http://localhost:18080/compras/recovery -H "Authorization: Bearer TOKEN" -H 'Content-Type: application/json' -d '{"operacion_id":"UUID_AMBIGUO"}'` <br>Verificar stock, reserva y compra antes/después |
| **Resultado esperado** | Recovery idempotente: si la operación se completó parcialmente, se completa o revierte de forma consistente; sin duplicados |
| **Datos a verificar** | Estado antes y después en `compras_db` y `eventos_db`; resultado del replay; idempotencia del endpoint |
| **Criterio de aprobación** | Un único resultado final coherente; sin duplicados; sin stock negativo |
| **Recuperación** | Si el recovery produce estado ambiguo, no repetir; conservar datos para investigación |
| **Estado** | ✅ **Probado** — recovery con PostgreSQL real probado en staging; idempotencia de recovery aprobada |

---

### Etapa 18 — Prueba de carga con Locust

| Campo | Detalle |
|---|---|
| **Objetivo** | Verificar comportamiento del sistema bajo carga sostenida; medir throughput, latencia y tasa de error |
| **Preparación** | Staging saludable; datos de prueba preexistentes (eventos con stock); archivo Locust de staging (`scripts/locust_safety.py`); no ejecutar en producción |
| **Comandos (staging)** | `locust -f scripts/locust_safety.py --host http://localhost:18080 --users 20 --spawn-rate 2 --run-time 120s --headless --only-summary` |
| **Resultado esperado** | Tasa de error < 1%; p95 latencia < 2s; 0 sobreventas; sin OOM ni crashes |
| **Datos a verificar** | RPS, p50/p95/p99 latencia, tasa de error, stock final vs compras registradas, logs de Compras1 y Compras2 |
| **Criterio de aprobación** | Sin sobreventa; errores dentro de umbral aceptado (409 de stock agotado no son errores de infraestructura); sistema estable al finalizar |
| **Recuperación** | Si hay sobreventa o crash, detener Locust inmediatamente; auditar antes de repetir |
| **Estado** | ❌ **No ejecutada** — pendiente; no sustituye pruebas de integridad; separar 409 de negocio de errores de infraestructura |

---

## Nota sobre Etapa de Dos PC (Etapas 19-20 conceptuales)

Las etapas de pérdida de PC-A y retorno de PC-A están **bloqueadas por infraestructura inexistente**. El diseño está documentado en `ARQUITECTURA_DOS_PC.md`. No ejecutar hasta tener:
- Dos equipos físicos disponibles
- Replicación PostgreSQL con quórum/fencing verificado
- Failover ensayado con datos sintéticos

| Etapa conceptual | Objetivo | Estado |
|---|---|---|
| Pérdida de PC-A | Apagar/desconectar PC-A; verificar supervivencia de PC-B con datos íntegros | ❌ Bloqueado por infraestructura |
| Retorno de PC-A | Reincorporar PC-A como standby; comparar LSN; reconciliar sin promover dos primarios | ❌ No implementado |

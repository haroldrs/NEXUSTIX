# Informe de evolución en Fedora — 3 de noviembre de 2026

Entrega actual: [EVOLUCION_DISTRIBUIDA](EVOLUCION_DISTRIBUIDA.md).
Pruebas y aceptación por escenario: [RESULTADOS_DISTRIBUIDO](RESULTADOS_DISTRIBUIDO.md).
Se amplió el flujo de negocio y se ejecutó un laboratorio separado; no se
alteraron los contenedores del staging previo ni se eliminaron sus volúmenes.
La fase VM quedó en archivos/guías, según la instrucción del usuario.

Para completar los apartados oficiales: [matriz de cumplimiento](MATRIZ_CUMPLIMIENTO_SISOPE2.md) y
[evidencias y exposición](PREPARACION_EXPOSICION.md). Portada, integrantes, bibliografía y resultados
finales están pendientes de los autores.

El informe que sigue corresponde a la entrega anterior de Windows. Sus
resultados y limitaciones temporales no sustituyen la evidencia nueva.

---

# Informe final de la entrega — NEXUSTIX (2026-10-25)

## Capacidades de la plataforma usada para esta entrega

| Capacidad | Disponible |
|---|---|
| Leer el ZIP, editar archivos, generar el ZIP final | Sí |
| Python 3.14 y Node 24 para análisis y pruebas | Sí |
| Docker / Docker Compose / WSL | **No** (Windows 11 sin Docker) |
| PostgreSQL real | Sí, tras autorización: binarios oficiales 15.19 de EnterpriseDB (`postgresql-15.19-5-windows-x64-binaries.zip`, 371 120 384 bytes, SHA-256 `49e06901…0143e`; los ejecutables del servidor no llevan firma Authenticode), servidor desechable en 127.0.0.1 eliminado al terminar |

Consecuencia: se ejecutaron análisis estático, pruebas unitarias/estáticas y
la integración sobre PostgreSQL real; nada que necesite contenedores se ejecutó.

## A. Cambios realizados

### Problemas corregidos (encontrados en la auditoría)

| # | Problema | Impacto | Corrección |
|---|---|---|---|
| 1 | `frontend/app.js` registraba los botones de inicio de sesión **dentro** de la función de compra. | En el entorno principal era imposible iniciar sesión y, por tanto, comprar. | Un único `app.js` (la versión corregida de staging) para ambos entornos; `app.staging.js` eliminado. |
| 2 | Nadie ejecutaba Alembic; Usuarios y Compras se niegan a arrancar sin la revisión. | Instalación limpia imposible. | `shared/run_migrations.py` antes de Uvicorn: crea o actualiza de forma aditiva, se niega ante revisiones desconocidas o esquemas legacy, serializa réplicas con advisory lock. |
| 3 | `compras/main.py` importaba `resource` sin usarlo. | 21 pruebas no podían ni importarse fuera de Linux. | Import eliminado. |
| 4 | `scripts/reset_entorno_pruebas.sh` truncaba datos con el proyecto Compose **por defecto** (principal) y dejaba claves idempotentes y journal huérfanos. | Riesgo de borrar datos de la demo; inconsistencia. | Movido a `legacy/`; reemplazo `nx.py pruebas reset-datos` (proyecto explícito, confirmación, limpia las tres tablas, siembra protegida; en demo exige un respaldo verificado). |
| 5 | Sin healthchecks en APIs, balanceador y frontend; `depends_on: service_started`. | Servicios "listos" sin poder atender. | Healthchecks funcionales (`/health` con `SELECT 1`; PostgreSQL por TCP + consulta) y `service_healthy`. |
| 6 | Sin `pool_pre_ping`. | Tras reiniciar PostgreSQL, la primera petición de cada conexión del pool fallaba con 500. | `shared/db_engine.py`: `pool_pre_ping`, `connect_timeout`, keepalives. |
| 7 | Nginx resolvía `compras1/2` solo al arrancar. | No arrancaba si faltaba una réplica; no seguía IPs nuevas. | Nginx 1.28 con `resolve` + resolver de Docker; el balanceador ya no depende de las réplicas. |
| 8 | `container_name` fijos y volúmenes dependientes del nombre de carpeta. | Colisiones entre entornos; datos "perdidos" al cambiar de carpeta. | Sin `container_name`; volúmenes y red con nombre explícito; todo por etiquetas Compose. |
| 9 | Contraseñas `postgres` fijas en el Compose principal; secretos solo para staging. | Credenciales débiles en demo. | `generar_secretos.py` para ambos entornos (contraseñas distintas por base, JWT 3072 bits, etiquetado SELinux). |
| 10 | Montajes del repositorio (`./frontend`, `nginx.conf`). | Fallan en Fedora con SELinux en Docker. | Frontend y balanceador se construyen como imágenes. |
| 11 | Auditoría con `stock_inicial` codificado. | Cualquier evento no base (p. ej. sintético de pruebas) salía inconsistente. | Columna `stock_inicial` en Eventos (aditiva) usada por auditoría y compensación. |
| 12 | `eventos/seed.py` restablecía stock aunque hubiera reservas. | Inconsistencia stock/compras. | Se niega si hay reservas activas. |
| 13 | Una réplica muerta sin cerrar TCP podía retener el bloqueo de su clave durante horas; la espera por la clave era ilimitada. | Bloqueo de reintentos. | Keepalives TCP en servidor y cliente, `idle_in_transaction_session_timeout=120s`, `lock_timeout` de 15 s ⇒ 503 reintentable. |
| 14 | Reloj de Lamport sin sincronización entre hilos. | Valores duplicados. | `Lock` en los tres servicios. |
| 15 | `restart: on-failure:5`. | Tras 5 fallos la réplica quedaba muerta para siempre. | `unless-stopped` (respeta paradas manuales). |
| 16 | Cookie de refresco `Secure` por defecto en HTTP. | El refresco falla con IPs de red. | `false` por defecto en demo HTTP, documentado; `true` con HTTPS. |
| 17 | Scripts de demostración pre-autenticación (`demo_sistemas_distribuidos.py`, `prueba_punto*.py`, `simular_trafico.py`). | Devolvían 401. | `simular_trafico.py` reescrito con JWT e idempotencia; el resto a `legacy/`. |
| 18 | Backup con nombres de contenedor fijos y sin verificación de esquema. | Podía dirigirse al entorno equivocado; no comprobaba restricciones ni revisión. | Destino por entorno + proyecto + etiquetas, primario detectado, huella de esquema y filas, comando `restore` solo sobre bases vacías. |
| 19 | (Hallado al ejecutar sobre PostgreSQL real) La huella de esquema nueva concatenaba `contype` (tipo `"char"`) sin conversión. | PostgreSQL rechaza esa concatenación por ambigua: todo respaldo habría fallado. | `contype::text`; cubierto por P8 y P11. |

### Funcionalidades nuevas

- `scripts/nx.py`: operación con entorno explícito (preflight, secretos, up con
  espera de salud, status, admin, verificar, auditoría, recuperación, respaldo,
  reset protegido, huella de no alteración). Nunca `down -v`.
- Replicación PostgreSQL (overlay) + `scripts/db_ha.py`: enable, status,
  promote con fencing, rejoin con archivo previo, switchback, sync on/off, disable.
- Imagen `postgres/` con rechazo de nodos aislados y clonado de standby.
- Reintento seguro en Nginx solo para `POST /compras` (`non_idempotent`).
- Recuperación automática en Compras (una réplica a la vez, *advisory lock*;
  solo avanza compras persistidas) y estado en `/health`.
- Ganchos de prueba de pausa y caída por checkpoint (incluye el nuevo
  `after_purchase_commit`), con marcador en el log; inertes fuera de pruebas.
- `scripts/verificar_flujo.py` (comprueba HTTP y persistencia en ambas bases).
- Suites `tests/postgres` (11 pruebas sobre PostgreSQL real desechable, incluidas
  replicación primario/standby y `pg_dump`/`pg_restore`) y
  `tests/resiliencia` (28 casos con Docker), ejecutor
  `scripts/ejecutar_pruebas.py` con clasificación APROBADA/FALLIDA/BLOQUEADA/PENDIENTE.
- Locust con contadores por réplica y auditoría final; `preparar_carga.py`.

### Decisiones arquitectónicas

1. Se conserva la arquitectura (servicios, contratos de API, saga e
   idempotencia existentes); solo se añaden campos (`recuperacion_automatica`
   en `/health`, `origen` y un contador en la recuperación).
2. Failover de PostgreSQL **manual** con fencing: sin tercer nodo no hay
   quórum seguro para automatizarlo.
3. La recuperación automática no libera stock; liberar sigue siendo una
   acción administrativa (no se cierran operaciones solo por antigüedad).
4. El entorno de pruebas existente (`nexustix-auth-staging`) se mantiene como
   entorno aislado; las suites históricas siguen siendo válidas.

### Archivos

- **Nuevos:** `postgres/*`, `compras_lb/Dockerfile`, `frontend/Dockerfile`,
  `frontend/nginx.conf`, `docker-compose.replicacion.yml`, `shared/{db_engine,healthcheck,run_migrations}.py`,
  `scripts/{nx,db_ha,entornos,pg_docker,generar_secretos,verificar_flujo,ejecutar_pruebas,preparar_carga}.py`,
  `tests/test_{configuracion,eventos_inventario,migraciones,operaciones,recuperacion_automatica}.py`,
  `tests/postgres/*`, `tests/resiliencia/*`, `config/*`, `docs/{ARQUITECTURA,BASES_DE_DATOS,INSTALACION_FEDORA,LIMITACIONES,PRUEBAS_RESILIENCIA,INFORME_FINAL}.md`,
  `requirements-load.txt`, `.dockerignore`, `.gitignore`, `.gitattributes`.
- **Modificados:** `compras/{main,database}.py`, `compras/Dockerfile`, migraciones `env.py`
  de usuarios y compras, `eventos/{main,models,seed,database}.py`, `eventos/Dockerfile`,
  `usuarios/{main,bootstrap_admin,database}.py`, `usuarios/Dockerfile`,
  `frontend/app.js`, `compras_lb/nginx.conf`, ambos Compose, `scripts/backup_main_databases.py`,
  `scripts/{prepare_staging,destroy_staging_secrets}.py`, `simular_trafico.py`, `locustfile.py`,
  `requirements-test.txt`, `README.md`, `docs/{GUIA_DEMOSTRACION,ARQUITECTURA_DOS_PC}.md`,
  y referencias en `tests/staging/*` y `tests/test_{authentication,backup_procedure}.py`.
- **Movidos (no borrados):** documentos antiguos a `docs/historico/`; scripts
  pre-autenticación, `multi-laptop/` y el log de diagnóstico a `legacy/`.
- **Eliminados:** `frontend/app.staging.js`, `frontend/nginx.staging.conf`
  (duplicados), cachés `__pycache__`/`.pytest_cache` y carpetas vacías
  `.git`, `.agents`, `.codex`, `.aws` del ZIP original.

## B. Resultados de pruebas

| Tipo | Detalle |
|---|---|
| Análisis estático | Lectura completa de servicios, Compose, Nginx, scripts y pruebas; `pyflakes`; validación YAML; `sh -n` de los scripts de shell; comprobación de que SQLAlchemy 2.0.30 (versión de las imágenes) admite URL multi-host. |
| Ejecutadas realmente | `python scripts/ejecutar_pruebas.py --capas unit postgres` con PostgreSQL 15.19 local → **114 APROBADAS, 0 FALLIDAS** (103 unit + 11 postgres; evidencia en `docs/evidencias/`). Línea base del ZIP original en la misma máquina: 19 aprobadas, 2 fallidas, 21 con error. Promoción del standby → primera compra correcta: 4,6 s. |
| No ejecutables aquí (BLOQUEADAS) | `tests/resiliencia` (28 casos), `tests/staging` (históricas), Locust, `docker compose config/up`, construcción de imágenes, orquestación Docker de `db_ha.py`. |
| A ejecutar en Fedora (PENDIENTES) | Todas las anteriores: `docs/PRUEBAS_RESILIENCIA.md` tiene comandos exactos, resultados esperados y criterios. |

## C. Estado de la resiliencia (diseñado; verificación con Docker pendiente)

| Situación | Comportamiento esperado |
|---|---|
| Cae Compras1 | Nginx reenvía al detectar el fallo; las compras siguen por Compras2; un `POST /compras` interrumpido se reenvía con la misma clave sin duplicar. Si el proceso muere, Docker lo reinicia; si se detiene a mano, queda detenido. |
| Cae Compras2 | Simétrico. |
| Caen ambas | 502 del balanceador, sin efectos en las bases; al volver, el reintento con la misma clave crea una sola compra. |
| Eventos no responde | 503 `recuperacion_requerida` con `operacion_id`; sin descuento de stock; reintento con la misma clave completa una única compra; timeouts acotados (~10 s por réplica). |
| Usuarios no responde | No se puede iniciar sesión ni renovar; las compras con token vigente continúan. |
| PostgreSQL se reinicia | Datos en volumen intactos; las APIs se reconectan solas (`pool_pre_ping`); peticiones durante el reinicio fallan con 5xx reintentable. |
| Se pierde la PC de la base | Sin replicación: servicio caído hasta restaurar respaldo en otra PC. Con el overlay en la misma PC: no protege. En dos PC (diseño, no desplegado): promoción manual del standby. |
| ¿Replicación real? | Sí, física en streaming por base. La mecánica de PostgreSQL y el SQL de `db_ha.py` se probaron con un primario y un standby reales (sin Docker); el overlay de Compose y el envoltorio de la imagen están **sin probar**. |
| ¿Failover real? | Manual y controlado (`db_ha.py promote` con fencing). Probado sin Docker: tras caer el primario y promover, la misma instancia de Compras volvió a comprar en 4,6 s sin reconfiguración. El fencing con Docker queda pendiente. **No** automático. |

## D. Instalación en una PC limpia

Requisitos, comandos, verificaciones y problemas conocidos:
`docs/INSTALACION_FEDORA.md`. Resumen: instalar Docker CE + plugin Compose,
`python3 scripts/nx.py demo preflight`, `… secretos`, `… up`, `… admin`,
`… status`, abrir `http://localhost:8080`.

## E. Preparación para la demostración

Guion, comandos por escenario, resultados esperados y evidencias a capturar:
`docs/GUIA_DEMOSTRACION.md`.

## F. Limitaciones

`docs/LIMITACIONES.md`: pruebas con Docker pendientes, failover de base
manual, instancias únicas de Usuarios/Eventos/balanceador, sin TLS, sin PITR.

## G. Evaluación final

| Nivel | Valoración | Justificación |
|---|---|---|
| Listo para probar | **Sí** | Instalación reproducible documentada, secretos locales, suites automatizadas con criterios y clasificación; 114 pruebas aprobadas (incluidas 11 sobre PostgreSQL real). |
| Listo para demostración controlada | **Condicionado** | Requiere ejecutar en Fedora `nx.py demo up` y la capa `resiliencia` al menos una vez: la configuración de contenedores (Nginx 1.28 `resolve`, envoltorio de PostgreSQL, migraciones automáticas) no se ha ejecutado nunca. |
| Listo para demostración académica completa | **No todavía** | Falta evidencia real de failover, recuperación, reinicio de PostgreSQL, respaldo y replicación en Fedora. Si `ejecutar_pruebas.py --capas unit postgres resiliencia` termina sin FALLIDAS, pasa a "listo". |
| No listo | — | No aplica: no hay defectos conocidos sin corregir, pero sí verificación pendiente. |

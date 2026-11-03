# Validación actual — Fedora, 3 de noviembre de 2026

Resultados nuevos, fallos corregidos y escenarios VM pendientes:
[RESULTADOS_DISTRIBUIDO](RESULTADOS_DISTRIBUIDO.md).

Se ejecutaron pruebas locales PostgreSQL y HTTP contra un laboratorio nuevo.
Las suites históricas de esta página están ligadas a `nexustix-auth-staging`:
**no se ejecutaron sus caídas contra la instalación existente**. Para repetir
la validación nueva usar `scripts/probar_local.py` y
`scripts/verificar_distribuido.py`, como indica
[EVOLUCION_DISTRIBUIDA](EVOLUCION_DISTRIBUIDA.md).

---

# Pruebas de resiliencia, consistencia y rendimiento

## Estados

- **APROBADA**: ejecutada y con resultado verificado.
- **FALLIDA**: ejecutada y no cumplió lo esperado.
- **BLOQUEADA**: no pudo ejecutarse por una dependencia ausente (se indica cuál).
- **PENDIENTE**: aún no ejecutada.

## Dónde se ejecutó qué (entrega del 2026-10-25)

La entrega se preparó en una PC **Windows 11 sin Docker** (sin WSL). Con
autorización del usuario se descargaron los binarios oficiales de
**PostgreSQL 15.19** de EnterpriseDB (`postgresql-15.19-5-windows-x64-binaries.zip`,
371 120 384 bytes, SHA-256 `49e0690194d9fe2bb4d7d6587fdddaa85a3eaaafe2fd5480e0916fcb25d0143e`;
los ejecutables del servidor de ese ZIP no llevan firma Authenticode) para un
servidor desechable en `127.0.0.1`, que se detuvo y eliminó al terminar.

| Capa | Ejecutada aquí | Resultado |
|---|---|---|
| `unit` — unitarias y estáticas (SQLite temporal, dobles HTTP, YAML/Nginx/scripts) + prueba Node del frontend | Sí (Python 3.14, Node 24) | **APROBADA** (103) |
| `postgres` — integración sobre PostgreSQL 15.19 real y desechable, incluida replicación primario/standby y `pg_dump`/`pg_restore` | Sí | **APROBADA** (11) |
| `resiliencia` — caídas reales en `nexustix-auth-staging` (Docker) | No | **BLOQUEADA** aquí (sin Docker); **PENDIENTE** en Fedora |
| `staging` — suites HTTP históricas (Docker) | No | **BLOQUEADA** aquí; **PENDIENTE** en Fedora |
| Locust | No | **PENDIENTE** |

Ninguna prueba con Docker se ha ejecutado contra esta versión (Compose,
imágenes, Nginx, envoltorio de PostgreSQL, orquestación de `db_ha.py`). Los
resultados de iteraciones anteriores están en `historico/` y corresponden a
otro código.

### Resultado observado (Windows, PostgreSQL 15.19 local, sin Docker)

Comando (con el servidor desechable ya iniciado):

```bash
export NEXUSTIX_TEST_PG_ADMIN_URL="postgresql://postgres:<aleatoria>@127.0.0.1:<puerto>/postgres"
export NEXUSTIX_TEST_PG_BIN="C:/Users/Kevin/.venvs/pg15/pgsql/bin"
python scripts/ejecutar_pruebas.py --capas unit postgres
```

Resultado: **114 APROBADAS, 0 FALLIDAS** — 102 casos pytest + 1 prueba Node en
`unit` y 11 en `postgres` (informe en `docs/evidencias/`). Capas `resiliencia`
y `staging` marcadas PENDIENTE por el propio informe.

Hallazgos de la ejecución sobre PostgreSQL real:

- **Defecto real corregido:** la huella de esquema del respaldo concatenaba
  `pg_constraint.contype` (tipo `"char"`) sin conversión; PostgreSQL rechaza
  `text || "char"` por ambiguo, así que **todo respaldo habría fallado** en
  Fedora. Ahora usa `contype::text` y la prueba P8 lo cubre.
- Ajuste de prueba: el *helper* de compras no traducía `HTTPException` a la
  respuesta que ve el cliente (FastAPI la convierte en 409/503).
- Promoción del standby → primera compra correcta en la **misma** instancia de
  Compras (mismo pool): **4,6 s**, conservando las compras previas.

En la auditoría inicial, la línea base del ZIP original daba en esta misma
máquina 19 aprobadas, 2 fallidas y 21 con error (por `import resource`,
módulo inexistente en Windows y sin uso, y por permisos POSIX en una aserción).

## Cómo ejecutar en Fedora

```bash
source ~/.venvs/nexustix-test/bin/activate          # ver INSTALACION_FEDORA.md §13
source config/pruebas.env.example
python3 scripts/nx.py pruebas up                     # entorno aislado
python scripts/ejecutar_pruebas.py --capas unit postgres resiliencia
# Opcionales:
sudo dnf install postgresql-server && export NEXUSTIX_TEST_PG_BIN=/usr/bin   # añade P10 y P11
NEXUSTIX_STAGING_RECOVERY_INTERVAL_SECONDS=10 python3 scripts/nx.py pruebas up   # habilita test_r17
python3 scripts/db_ha.py pruebas enable && python scripts/ejecutar_pruebas.py --capas resiliencia   # test_r21
python3 scripts/db_ha.py pruebas disable && python scripts/ejecutar_pruebas.py --capas staging       # suites históricas (9 servicios)
```

Cada corrida crea `resultados/<fecha UTC>/` (no borra corridas previas). Las
pruebas usan eventos sintéticos de id ≥ 10 000 000 con su propio
`stock_inicial` y compradores `qa-*@example.com`; quedan como evidencia y no
afectan la auditoría. Para empezar de cero:
`python3 scripts/nx.py pruebas reset-datos --confirmar nexustix-auth-staging`.

## Matriz

| ID | Escenario (sección del enunciado) | Archivo / prueba | Estado |
|---|---|---|---|
| U1 | Idempotencia: replay, otra clave, mismo cuerpo, concurrencia (SQLite) | `tests/test_authentication.py` | APROBADA |
| U2 | Recuperación: replay pendiente, NOWAIT, claim sin reserva, compensación repetible | `tests/test_authentication.py` | APROBADA |
| U3 | Recuperación automática nunca libera stock; admin sí; repetición sin efectos | `tests/test_recuperacion_automatica.py` | APROBADA |
| U4 | Ganchos de caída inertes salvo habilitación explícita | `tests/test_recuperacion_automatica.py` | APROBADA |
| U5 | Inventario: `stock_inicial`, compensación acotada, seed protegido | `tests/test_eventos_inventario.py` | APROBADA |
| U6 | Migraciones protegidas: matriz, Alembic real con conexión compartida, legacy rechazado | `tests/test_migraciones.py` | APROBADA |
| U7 | Configuración: healthchecks, dependencias, aislamiento demo/pruebas, Nginx, frontend, sin secretos ni `down -v` | `tests/test_configuracion.py` | APROBADA |
| U8 | Operación: fencing antes de promover, sin dos primarios, confirmaciones destructivas | `tests/test_operaciones.py` | APROBADA |
| U9 | Respaldo: destino explícito, etiquetas, dos primarios, esquema/filas, hash | `tests/test_backup_procedure.py` | APROBADA |
| P1 | Instalación nueva: migraciones reales a la revisión esperada e idempotentes | `tests/postgres` | APROBADA |
| P2 | Dos réplicas migrando a la vez (advisory lock) | `tests/postgres` | APROBADA |
| P3 | Última entrada concurrente sobre PostgreSQL real (§7.2) | `tests/postgres` | APROBADA |
| P4 | Misma clave desde ambas réplicas en paralelo (§7.3, §7.5) | `tests/postgres` | APROBADA |
| P5 | Eventos caído → 503 ambiguo → reintento único (§7.9) | `tests/postgres` | APROBADA |
| P6 | Clave retenida por otra réplica → 503 reintentable (lock_timeout) | `tests/postgres` | APROBADA |
| P7 | Recuperación omite operación viva; barrido exclusivo entre réplicas | `tests/postgres` | APROBADA |
| P8 | SQL operativo (respaldo, reset) válido en PostgreSQL 15 | `tests/postgres` | APROBADA |
| P9 | Auditoría consistente tras varias compras sobre PostgreSQL real | `tests/postgres` | APROBADA |
| P10 | Replicación real primario/standby: SQL de `db_ha.py` (preparar, status, sync, promote), standby de solo lectura, `target_session_attrs` y failover de las APIs tras caída del primario | `tests/postgres/test_pg_replicacion.py` | APROBADA (sin Docker) |
| P11 | `pg_dump`/`pg_restore` reales conservan agregados y huella de esquema | `tests/postgres/test_pg_respaldo.py` | APROBADA |
| R01 | Inicio limpio del entorno aislado (proyecto, red, volúmenes, puertos) | `test_01_entorno.py` | PENDIENTE |
| R02 | Healthchecks funcionales; base caída ⇒ API 503 con proceso vivo | `test_01_entorno.py` | PENDIENTE |
| R03 | Flujo normal hasta PostgreSQL (§7.1) | `test_02_compras.py` | PENDIENTE |
| R04 | Idempotencia entre réplicas, mismo y distinto contenido (§7.3–7.5) | `test_02_compras.py` | PENDIENTE |
| R05 | Última entrada con 8 compradores concurrentes (§7.2) | `test_02_compras.py` | PENDIENTE |
| R06 | Caída abrupta de Compras1 (SIGKILL) y reincorporación | `test_03_failover_replicas.py` | PENDIENTE |
| R07 | Caída abrupta de Compras2 y reincorporación | `test_03_failover_replicas.py` | PENDIENTE |
| R08 | Ambas réplicas caídas: sin efectos; recuperación de ambas | `test_03_failover_replicas.py` | PENDIENTE |
| R09 | Caída antes de crear la reserva (`before_claim`, `after_claim`) (§7.6) | `test_04_caidas_en_vuelo.py` | PENDIENTE |
| R10 | Caída tras reservar y antes de confirmar (`after_reservation`, `after_purchase_commit`) (§7.7) | `test_04_caidas_en_vuelo.py` | PENDIENTE |
| R11 | Caída tras confirmar y antes de responder (`after_final_response_commit`) (§7.8) | `test_04_caidas_en_vuelo.py` | PENDIENTE |
| R12 | Reinicio automático del proceso con solicitud activa; failover transparente del LB (§7.12) | `test_04_caidas_en_vuelo.py` | PENDIENTE |
| R13 | Usuarios no disponible | `test_05_dependencias.py` | PENDIENTE |
| R14 | Eventos detenido (§7.9) | `test_05_dependencias.py` | PENDIENTE |
| R15 | Eventos congelado: timeouts acotados, respuesta perdida | `test_05_dependencias.py` | PENDIENTE |
| R16 | Reserva huérfana compensada una vez; recuperación repetible (§7.10–7.11) | `test_06_recuperacion.py` | PENDIENTE |
| R17 | Recuperación automática confirma compras persistidas | `test_06_recuperacion.py` | PENDIENTE (requiere intervalo > 0) |
| R18 | Reinicio de las tres PostgreSQL conservando datos | `test_07_postgres.py` | PENDIENTE |
| R20 | Respaldo verificado y restaurado en aislamiento | `test_08_respaldo.py` | PENDIENTE |
| R21 | Replicación, promoción con fencing, reincorporación y retorno | `test_09_replicacion.py` | PENDIENTE (requiere `db_ha.py enable`) |
| R22 | Verificación final de stock, compras, reservas y registros | `test_99_final.py` | PENDIENTE |
| R23 | El entorno demo no fue alterado | `test_99_final.py` | PENDIENTE |
| S* | Suites históricas de autenticación, failover, recovery y caída abrupta | `tests/staging/` | PENDIENTE |
| L1 | Carga con Locust | `locustfile.py` | PENDIENTE |

## Especificación de las pruebas con Docker

Cada prueba documenta en su *docstring* objetivo, preparación, resultado
esperado y criterio; aquí el resumen con los comandos.

**Preparación común:** `python3 scripts/nx.py pruebas up`;
`export NEXUSTIX_STAGING_HTTP=1`. **Comando:**
`python -m pytest -v tests/resiliencia/<archivo>::<prueba>`. **Limpieza
común:** cada prueba vuelve a arrancar lo que detuvo (bloques `finally`),
recrea `compras1` sin ganchos y deja sus datos sintéticos como evidencia.
Ninguna prueba toca el proyecto `nexustix`.

### R06/R07 — Caída de una réplica
- *Preparación:* evento sintético con stock 20.
- *Acción:* `docker kill --signal KILL <id de compras1|2>` (id por etiquetas).
- *Esperado:* `ExitCode=137`, Docker no la reinicia (parada manual); 6 compras
  por el LB con 200 atendidas por la superviviente; al arrancarla, el LB
  vuelve a usar ambas.
- *Criterio:* 6 filas en `compras`, stock 14, ambas instancias en `atendido_por`.

### R09–R11 — Caída en cada checkpoint de la saga
- *Preparación:* `compras1` recreada con `NEXUSTIX_TEST_CRASH_HOOKS=1`.
- *Acción:* `POST` directo a `:18083` con `X-NEXUSTIX-Test-Hold-At: <checkpoint>`;
  al ver `NEXUSTIX_TEST_HOLD checkpoint=…` en el log, `docker kill` del
  contenedor; luego reintento con la misma clave por el LB.
- *Esperado antes del reintento:* `before_claim` sin clave; `after_claim`
  clave `processing`; `after_reservation` clave `processing` + reserva;
  `after_purchase_commit` y `after_final_response_commit` compra + clave `completed`.
- *Esperado después:* 200, una compra, reserva `confirmada`, stock −1. En
  `after_purchase_commit` el replay dice `pendiente` y `/compras/recuperar-servidor`
  lo confirma; una segunda recuperación no vuelve a actuar.

### R12 — Reinicio automático vs failover
- `X-NEXUSTIX-Test-Crash-At: after_reservation` (el proceso hace `os._exit(86)`):
  el **mismo** contenedor reinicia solo (`RestartCount +1`). Por el LB, la
  misma cabecera produce una respuesta 200 con dos direcciones en
  `X-Nexustix-Upstream`: Nginx reenvió la petición a `compras2`, que reanudó la
  misma `operacion_id`.

### R13–R15 — Dependencias
- Usuarios detenido: login sin conexión, compra con token vigente = 200.
- Eventos detenido: 503 `recuperacion_requerida` con `operacion_id`, clave
  `processing`, sin reserva ni compra; tras arrancar Eventos el reintento crea
  una compra con la misma `operacion_id`.
- Eventos congelado (`docker pause`): 503 en < 30 s; al reanudar puede haber
  procesado la reserva en cola; el reintento la reutiliza (stock −1 exacto).

### R18 — PostgreSQL
- `docker restart` (compras_db, usuarios_db) y `stop`/`start` (eventos_db) del
  contenedor exacto: conteos conservados, mismo contenedor, compra posterior
  correcta reintentando con la misma clave, Usuarios acepta registros.

### R20 — Respaldo
- `nx.py pruebas respaldo --destino <tmp> --ensayar-migraciones`: manifiesto
  `complete`, SHA-256, huella de esquema, `verification.json` `verified` con
  `counts_match` y `schema_match` en las tres bases, recursos temporales eliminados.

### R21 — Replicación
- Tres bases `REPLICADO`; la compra llega al standby; `promote compras_db`
  aísla el nodo (detenido, reinicio `no`, arranque rechazado con código 64);
  las APIs escriben en el nuevo primario sin reconfigurar; `rejoin` y
  `switchback` devuelven el estado normal; nunca hay dos primarios. Imprime el
  tiempo desde la promoción hasta la primera compra correcta.

### R22/R23 — Cierre
- Auditoría `todo_consistente`, diferencia global 0, sin reservas pendientes ni
  claves `processing` de la suite; huella de solo lectura del entorno demo
  idéntica antes y después.

## Rendimiento (Locust)

```bash
python -m pip install -r requirements-load.txt
python3 scripts/preparar_carga.py --stock 300        # evento y comprador sintéticos nuevos
source ~/.config/nexustix-locust.env
mkdir -p resultados/locust
locust -f locustfile.py --host http://localhost:18085 --headless -u 20 -r 5 -t 2m --csv resultados/locust/carga
```

`locust_safety.py` rechaza cualquier destino que no sea el loopback de pruebas
(18085/18081). Al terminar se imprimen respuestas por código, compras por
réplica y la fila de auditoría del evento sintético (stock inicial = actual +
vendidas). Para observar la caída de una réplica bajo carga, ejecute en otra
terminal `python3 scripts/nx.py pruebas stop compras1` y después `start`.

Métricas a registrar: p50/p95/p99 y errores (`*_stats.csv`), 409 esperados al
agotar stock, compras por réplica y la consistencia final. **No se ha
ejecutado Locust en esta entrega.** Limitaciones: una sola PC genera la carga
y aloja el sistema (compiten por CPU); un único comprador sintético; los
resultados no son extrapolables a producción.

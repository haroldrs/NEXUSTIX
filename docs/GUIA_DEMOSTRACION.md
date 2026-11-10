# Demostración del checkout distribuido — Fedora

La interfaz actual reúne observabilidad y laboratorio dentro de **Administración**.
Consulta [Portal y operaciones](EVOLUCION_PORTAL_Y_OPERACIONES.md) para las
migraciones preparadas, la configuración del mapa y las comprobaciones manuales
pendientes. Ningún procedimiento de esta guía se ejecutó durante esos cambios.

Usar `http://localhost:28080` del laboratorio nuevo. No necesita VMs.

1. Ingresar como `lab-admin@example.com` con el secreto `admin-password` del
   directorio `~/.config/nexustix/distributed-lab-secrets`.
2. Administración: crear evento con fecha, capacidad y precio. Opcionalmente
   ingresar una línea `A1,VIP,50.00` por asiento; capacidad = número de asientos.
3. Registrar un comprador, seleccionar evento/cantidad/asientos y reservar.
   Para un grupo añadir varias selecciones al pedido antes de enviarlo.
4. Mis pedidos: aprobar el pago simulado. Actualizar para ver las entradas QR.
   Repetir un pago mantiene el mismo resultado; no genera entradas adicionales.
5. Administración: validar el código de una entrada y repetirlo: el segundo
   intento se rechaza. Para probar reembolso usar otro pedido sin entradas usadas.
6. Crear otro pedido y rechazar el pago: verificar devolución del stock.
7. Observabilidad: consultar Pedidos y PostgreSQL para ver estados persistidos,
   inventario, rol de base, streaming, lag y archivador. No son valores simulados.
8. Para evidencia de caída real usar el script de laboratorio y revisar su
   informe; no apagar contenedores de staging ni VMs durante esta demostración.

Referencia y comandos: [EVOLUCION_DISTRIBUIDA](EVOLUCION_DISTRIBUIDA.md).
La guía siguiente corresponde a la demostración legacy.

---

# Guía de demostración académica — NEXUSTIX

Duración sugerida: 20–25 minutos. Todas las pruebas destructivas (caídas,
promoción de base de datos) se hacen en el **entorno de pruebas**
(`nexustix-auth-staging`, puertos 18080–18085); el entorno **demo**
(puertos 8080, 8001–8005) se usa para mostrar la aplicación.

## 0. El día anterior

```bash
cd ~/proyectos/NEXUSTIX
python3 scripts/nx.py demo preflight && python3 scripts/nx.py pruebas preflight
python3 scripts/nx.py demo up && python3 scripts/nx.py demo admin --email admin@example.com
python3 scripts/nx.py pruebas up && python3 scripts/nx.py pruebas admin --email qa-admin@example.com
python3 scripts/nx.py pruebas verificar --compra
python scripts/ejecutar_pruebas.py --capas unit postgres resiliencia   # con el venv de pruebas
python3 scripts/nx.py demo respaldo --destino ~/nexustix-backups/antes-de-la-demo
```

Guarde `resultados/<fecha>/RESUMEN.md`: es la evidencia de las pruebas.
Para la parte opcional de base de datos: `python3 scripts/db_ha.py pruebas enable`.

## 1. Arquitectura (2 min)

Mostrar `docs/ARQUITECTURA.md` §1 y `python3 scripts/nx.py demo status`:
nueve contenedores *healthy*, health HTTP de cada API con `db=connected`,
rol de cada PostgreSQL.

**Evidencia:** captura de `status`.

## 2. Flujo de compra en el frontend (4 min)

1. `http://localhost:8080` → catálogo desde Eventos.
2. "Ingresar / registrarse" → crear cuenta compradora.
3. Elegir evento y cantidad → "Continuar con la compra" → mensaje
   *Compra confirmada · … · Operación <id>*. "Mis compras" la lista.
4. Iniciar sesión como administrador → "Observabilidad": servicios, métricas
   por réplica, auditoría **Consistente** y journal con el `COMMIT`.

**Evidencia:** capturas del mensaje de compra y de la auditoría.

## 3. Idempotencia (3 min, terminal, entorno de pruebas)

```bash
python3 scripts/nx.py pruebas verificar --compra
```

Muestra: compra por el LB, replay con la misma clave (misma compra,
`Idempotency-Replayed`), misma clave con otro cuerpo (409) y la comprobación
en ambas bases (una compra, reserva confirmada, stock −1).

## 4. Caída de una réplica de Compras (4 min)

Terminal A (tráfico continuo, comprador sintético):

```bash
python3 simular_trafico.py --entorno pruebas --evento 1 --compras 40 --pausa 0.5
```

Terminal B, mientras corre:

```bash
python3 scripts/nx.py pruebas stop compras1      # parada controlada
python3 scripts/nx.py pruebas status             # compras1 exited; LB y compras2 sanos
python3 scripts/nx.py pruebas start compras1
```

Esperado: las compras siguen con 200 atendidas por la instancia 2; si alguna
petición cae justo durante la parada, el script la reintenta **con la misma
clave** y no se duplica. Al volver compras1, el LB reparte de nuevo.
El resumen final indica compras por réplica y reintentos.

Caída abrupta reproducible (opcional): `python -m pytest -v tests/resiliencia/test_04_caidas_en_vuelo.py -k r12b`
muestra una petición atendida por dos upstreams (Nginx reenvió la misma
compra a la otra réplica tras la muerte del proceso) y una sola compra.

**Evidencia:** salida de `simular_trafico.py` y de `status`.

## 5. Dependencia caída: Eventos (3 min)

```bash
python -m pytest -v tests/resiliencia/test_05_dependencias.py -k r14
```

Explicar: sin Eventos la compra responde 503 *recuperacion_requerida* con su
`operacion_id`; no hay reserva ni compra ni descuento de stock; al volver
Eventos, el reintento con la misma clave completa exactamente una compra.
Comparar con Usuarios caído (`-k r13`): las compras siguen porque el JWT se
valida localmente.

## 6. Recuperación de operaciones incompletas (3 min)

```bash
python -m pytest -v tests/resiliencia/test_04_caidas_en_vuelo.py -k after_purchase_commit
```

Explicar la tabla de ventanas de fallo (`ARQUITECTURA.md` §3): la réplica
muere después de guardar la compra y antes de confirmar la reserva; el
replay dice `pendiente`; `/compras/recuperar-servidor` la confirma; repetir la
recuperación no cambia nada. A mano:
`python3 scripts/nx.py pruebas recuperar --email qa-admin@example.com`.

## 7. PostgreSQL: persistencia, respaldo y replicación (4 min)

```bash
python -m pytest -v tests/resiliencia/test_07_postgres.py -k compras_db   # reinicio sin pérdida
ls ~/nexustix-backups/antes-de-la-demo && cat ~/nexustix-backups/antes-de-la-demo/verification.json
```

Opcional con replicación habilitada en pruebas:

```bash
python3 scripts/db_ha.py pruebas status
python3 scripts/db_ha.py pruebas promote compras_db   # aísla compras_db y promueve la réplica
python3 scripts/nx.py pruebas verificar --compra      # las APIs ya escriben en el nuevo primario
python3 scripts/db_ha.py pruebas rejoin compras_db compras_db
python3 scripts/db_ha.py pruebas switchback compras_db
```

Explicar con honestidad: es **promoción manual controlada** con fencing; no
hay failover automático (dos nodos no tienen quórum) y todo corre en una PC.

## 8. Cierre (1 min)

Panel de Observabilidad (auditoría consistente) y
`python3 scripts/nx.py demo huella` para mostrar que las pruebas no tocaron
el entorno demo. Limitaciones: `docs/LIMITACIONES.md`.

## Qué decir (y qué no)

| Sí | No |
|---|---|
| "Failover entre dos réplicas de Compras detrás de Nginx, con reintento seguro por clave idempotente." | "Alta disponibilidad de extremo a extremo." |
| "Saga con reserva/confirmación/compensación y recuperación basada en evidencia." | "Transacción ACID distribuida." |
| "Replicación PostgreSQL con promoción manual y fencing." | "Failover automático de la base de datos." |
| "Docker reinicia procesos caídos; la PC sigue siendo un punto único de fallo." | "Tolera la caída de la computadora." |

## Si algo falla durante la demo

- `python3 scripts/nx.py <entorno> status` y `logs <servicio>`.
- Un servicio no *healthy*: `python3 scripts/nx.py <entorno> up` (no borra datos).
- Auditoría con reservas pendientes:
  `python3 scripts/nx.py <entorno> recuperar --email <admin>` y después
  `python3 scripts/nx.py <entorno> auditoria --email <admin>`.
- Tras una promoción a medias: `python3 scripts/db_ha.py pruebas status` y
  `rejoin`/`switchback`.
# Laboratorio de Resiliencia (panel administrativo)

1. Inicie sesión como administrador y abra **Administración → Laboratorio de Resiliencia**. La vista consulta los `/health` reales de Usuarios, Eventos, ambas réplicas de Compras y el balanceador; no ejecuta acciones destructivas.
2. Para demostrar caída de una réplica de Compras, en el entorno de laboratorio ejecute `docker compose stop compras1`, actualice el laboratorio y realice una lectura o compra con una clave idempotente. Restaure con `docker compose start compras1`; el panel mostrará el retorno de la réplica y el journal/recuperación permite observar el trabajo pendiente.
3. Usuarios y Eventos se demuestran deteniendo y restaurando sólo sus contenedores en la topología que tenga réplica real. En el `docker-compose.yml` principal no hay réplica de esos servicios: el panel lo mostrará como indisponibilidad, no como alta disponibilidad.
4. PostgreSQL, gateway o PC completa son puntos únicos de fallo salvo que se inicie explícitamente el overlay de replicación/documentación de VM. Use los procedimientos de `despliegue-vm/` para esos casos; la aplicación sólo visualiza su evidencia.

La página pública no puede detener servicios. Las cancelaciones de eventos se ejecutan desde Compras para exigir autorización administrativa y preservar el progreso de compensación.

---

# Guía académica integral

Secuencia para mostrar NEXUSTIX desde el panel, con la terminal solo cuando un procedimiento
manual lo requiera. **Antes de la exposición, reconstruye las imágenes y ensaya cada paso:** las
funciones de QR, escáner y paneles nuevos aún no se han ejecutado. Registra lo que ocurra; no
adelantes un resultado que no hayas observado.

## A. Demostración comercial

1. Abre el portal y consulta los eventos.
2. Abre un evento con zonas o asientos numerados y muestra la selección visual.
3. Elige una entrada y reserva.
4. En **Mis entradas**, completa el pago simulado y consulta el pedido.
5. Muestra el ticket con su QR.
6. Con una cuenta organizadora del evento, abre **Control de acceso** y valida.
7. Repite el mismo código: debe rechazarse como entrada ya utilizada.

La cámara requiere HTTPS o `localhost`. En LAN por HTTP usa el ingreso manual del código.

## B. Demostración distribuida

1. Abre **Administración → Centro de Control**.
2. Recorre el mapa: computadoras y servicios.
3. En **Observabilidad**, pulsa **Consultar instancias**.
4. Señala las conexiones HTTP en el mapa.
5. En **Seguir una compra o pedido**, consulta el pedido de la demostración.
6. Abre **PostgreSQL y persistencia** y explica primarias, standbys y WAL.

## C. Concurrencia: dos compradores, un asiento

- **Condición de carrera:** dos solicitudes leen el asiento libre antes de que alguna escriba.
- **Bloqueo:** Eventos toma `FOR UPDATE` sobre la fila del evento, lo que serializa las reservas.
- **Transacción:** comprobación y asignación se confirman juntas.
- **Resultado esperado:** una reserva aceptada y otra con conflicto de disponibilidad.
- **Evidencia necesaria:** ambas operaciones en **Operaciones concurrentes** y **Inventario por
  evento** consistente. La fecha de creación no prueba que hayan coincidido en el tiempo.

## D. Tolerancia a fallos

Se realizan manualmente, en un entorno aislado y con respaldo. Antes, en el Laboratorio, captura
el estado inicial; después de la falla, captura de nuevo; al terminar, guarda el reporte.

| Escenario | Requisitos | Riesgo | Estado inicial | Procedimiento autorizado | Resultado esperado | Registrar | Recuperación | Criterio de éxito | Limitación |
|---|---|---|---|---|---|---|---|---|---|
| Fallo de una API (B) | Dos réplicas | Medio | Ambas responden | Detener manualmente una réplica | Otra atiende las solicitudes nuevas | Instancia que respondió | Iniciar la réplica | Vuelve a responder sin duplicar compras | Las solicitudes en vuelo pueden fallar |
| Fallo de base primaria (H) | Respaldo y entorno aislado | Alto | Rol observado primaria | Detener la base en el entorno de pruebas | Operaciones de ese servicio bloqueadas | Mensajes y bases afectadas | `despliegue-nodos/RECUPERACION.md` | Rol, WAL y operaciones verificados | Sin promoción automática |
| Pérdida de Fedora (I) | Dos PC, inventario vigente | Alto | Proyección revisada | Apagar la máquina de forma controlada | Según el inventario: se pierden sus primarias y sus APIs | Componentes sin respuesta frente a la proyección | Encender y verificar; promoción manual si corresponde | Datos y operaciones verificados | Las standbys no se promocionan solas |
| Pérdida de Windows (I) | Dos PC, comprobar que Docker Desktop se detuvo | Alto | Proyección revisada | Apagar la máquina de forma controlada | Según el inventario: se pierde la primaria de Eventos | Operaciones afectadas | Igual que el anterior | Inventario y pedidos verificados | Detener solo Ubuntu no aísla Docker Desktop |
| Fallo del gateway (O) | Otra dirección conocida | Medio | Dirección alternativa anotada | Detener el gateway en uso | La otra dirección funciona | Dirección usada y respuesta | Iniciar el gateway | Acceso confirmado por la alternativa | Sin redirección automática |
| Operación interrumpida (P) | Pedido autorizado | Medio | Pedido identificado | Interrumpir un paso manualmente | El pedido continúa o se compensa | Secuencia en **Recuperación de pedidos** | Trabajo de recuperación existente | Estado final sin duplicados | Recuperar la base no reconcilia todo |

## E. Una PC o varias PC

| Demostración | Una PC | Varias PC |
|---|---|---|
| Flujo comercial, QR y roles | Sí | Sí |
| Concurrencia entre procesos | Sí | Sí |
| Fallo de una instancia | Sí, en Compras con el Compose local | Sí |
| Replicación entre computadoras | No | Sí |
| Pérdida de una computadora | No: apagarla detiene todo | Sí |
| Gateway alternativo | No | Sí |

Una PC no tolera su propio apagado, aunque ejecute varios contenedores.

## F. Preguntas del profesor

- **¿Por qué microservicios?** Para separar dominios con bases propias, replicar procesos y
  estudiar fallos parciales. A cambio, no hay transacción común.
- **¿Por qué PostgreSQL?** Ofrece transacciones, bloqueos de fila, restricciones y replicación por WAL.
- **¿Replicación o respaldo?** La réplica copia el presente, incluidos los errores; el respaldo es
  una copia recuperable que debe verificarse.
- **¿Qué es una Saga?** Una secuencia de transacciones locales con compensaciones si un paso falla.
- **¿Por qué no una transacción global?** Compras y Eventos tienen bases distintas. Una 2PC acoplaría
  su disponibilidad; NEXUSTIX acepta convergencia con estados durables.
- **¿Cómo evitan vender dos veces un asiento?** La reserva se serializa con un bloqueo de la fila del
  evento y comprueba que el asiento siga libre en la misma transacción.
- **¿Qué ocurre si falla una API?** HAProxy la retira tras sus comprobaciones. Otra réplica atiende si
  conserva sus dependencias.
- **¿Qué sucede si se apaga Fedora?** En el despliegue conocido se pierden las primarias de Usuarios y
  Compras y una standby de Eventos. Autenticación y compras quedan afectadas hasta la recuperación manual.
- **¿Y si se apaga Windows?** Se pierde la primaria de Eventos, de la que dependen el catálogo y las reservas.
- **¿Por qué una standby no se vuelve primaria sola?** Sin consenso ni fencing podría haber dos escritoras.
- **¿Qué es split-brain?** Dos nodos aceptan escrituras divergentes como si cada uno fuera el legítimo.
- **¿Por qué quórum?** Solo una mayoría puede decidir, y dos particiones no pueden tener mayoría a la vez.
- **¿Qué es fencing?** Impedir de forma verificable que el escritor anterior siga escribiendo.
- **¿Rollback o compensación?** El rollback revierte una transacción local no confirmada; la
  compensación es una operación nueva que revierte un efecto ya confirmado.
- **¿Qué garantiza la replicación asíncrona?** Que los standbys reciben los cambios con retraso. No
  garantiza pérdida cero ni lecturas actualizadas.
- **¿Qué falta para alta disponibilidad automática?** Coordinador real en tres dominios físicos, fencing
  verificable, acceso redundante y ensayos registrados.

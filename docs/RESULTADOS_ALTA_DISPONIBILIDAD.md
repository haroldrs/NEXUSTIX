# Resultados: alta disponibilidad y coordinación distribuida — 13/11/2026

Pruebas ejecutadas en Linux con Docker 29. Las computadoras se simularon con
contenedores en redes Docker aisladas; **todavía falta repetirlas con las PC
reales del laboratorio** (ver la guía de [ALTA_DISPONIBILIDAD.md](../despliegue-nodos/ALTA_DISPONIBILIDAD.md)).

## 1. Failover automático de PostgreSQL (2 PC + árbitro)

Comando: `python3 tests/ha/laboratorio_patroni.py`. Usa la configuración que
genera `despliegue-nodos/render.py` para el inventario de dos PC y un árbitro:
3 votantes etcd y la base `compras_db` con Patroni en pc1 y pc2, replicación síncrona.

| Escenario | Qué se hizo | Resultado | Medición |
|---|---|---|---|
| Arranque | pc1 (principal declarada) inicia el clúster; pc2 se clona | Aprobado | pc2 réplica síncrona en 26.9 s |
| Caída total de la PC principal | Se detienen de golpe la base y el votante etcd de pc1 | Aprobado | Réplica promovida en **27.9 s**; escritura posible en **28.2 s** (RTO). **50 de 50** filas confirmadas conservadas (RPO = 0) |
| Reincorporación | pc1 vuelve a encenderse | Aprobado | Vuelve como **réplica síncrona** en 23.4 s, con la fila escrita durante su ausencia (pg_rewind) |
| Partición de red | La principal (pc2) queda aislada de las demás | Aprobado | La aislada pasa a **solo lectura** en 26.8 s; pc1 + árbitro promueven a pc1; **nunca hubo dos principales**; la escritura en la aislada fue rechazada. Al volver la red, pc2 se reincorpora en 14.9 s |

Errores encontrados y corregidos durante las pruebas:

* `pg_rewind` no tenía credencial (`PATRONI_REWIND_PASSWORD`): la antigua
  principal no podía volver como réplica. Corregido en `despliegue-nodos/patroni/entrypoint.sh`.
* La copia inicial de una réplica esperaba un checkpoint lento (minutos):
  ahora usa `checkpoint: fast`.
* Faltaba una regla `pg_hba` para la conexión de replicación local que usa Patroni.

Observación importante: con replicación síncrona, Patroni solo promueve a la
réplica marcada como **síncrona**. En una corrida se apagó la principal segundos
después del arranque, cuando la réplica aún no era síncrona, y Patroni no la
promovió. Es el comportamiento correcto (evita perder compras), pero hay que
esperar a que la réplica figure como síncrona antes de una demostración.

## 2. Aplicación completa en contenedores (una PC)

Entorno `demo` (12 contenedores) con las imágenes de esta versión.

| Prueba | Resultado |
|---|---|
| Directorio de nombres | Las 4 instancias (usuarios, eventos, compras_1, compras_2) aparecen vivas; `resolver compras` devuelve las dos réplicas |
| Elección Bully | Coordinador inicial: compras_1 (prioridad mayor) |
| Caída simulada del coordinador | compras_1 pasa a sospechosa y luego a caída; compras_2 asume como coordinador en 6–9 s |
| Restablecimiento | compras_1 convoca elección y recupera el rol (mayor prioridad) |
| Relojes en la Saga | Cada reserva y confirmación muestra envío → recepción → respuesta → recepción de respuesta, con Lamport creciente y vectores que incluyen a ambos servicios |
| Cristian | Desfase estimado −0.25 ms con RTT 3.1 ms contra el coordinador |
| Seguimiento del pedido | El comprador ve los 5 pasos (creado, apartado, pagado, emitiendo, emitido) con hora, instancia y reloj lógico |

## 3. Concurrencia: 30 compradores, 5 asientos

Comando: `python3 scripts/prueba_concurrencia.py --gateway 127.0.0.1 --gateway localhost --capacidad 5 --compradores 30 …`

| Dato | Valor |
|---|---|
| Compras confirmadas | **5** (exactamente la capacidad) |
| Rechazadas por falta de disponibilidad | 25 |
| Inventario | inicial 5 = disponibles 0 + reservadas 0 + vendidas 5 → **consistente** |
| Latencia máxima | 682 ms |

Con dos PC reales, pasar la IP de cada gateway en `--gateway` reparte los
compradores entre ambas computadoras.

## 4. Cambio automático de gateway en el navegador

Prueba con Chromium (Playwright): la página se abrió por un gateway y luego se
bloquearon todas sus API. La consulta de eventos se completó por el otro
gateway (7 eventos), apareció el aviso "La computadora … dejó de responder.
Seguimos atendiéndote desde …" y las solicitudes siguientes fueron directamente
al nuevo gateway.

## 5. Pruebas automáticas

`python3 -m pytest -q`: 160 aprobadas. Siguen fallando 2 pruebas que ya fallaban
antes de estos cambios (comprueban textos antiguos del frontend):
`test_purchase_frontends_send_idempotency_key_and_cors_allows_it` y
`test_frontend_registers_login_on_load_not_after_a_purchase`.

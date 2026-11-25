# Resultados de las pruebas

Las pruebas de caída se ejecutaron en un entorno que reproduce cada computadora
con contenedores en redes Docker aisladas y una IP propia. El despliegue en tres
computadoras físicas (Fedora y dos Windows con WSL2, unidas por Tailscale) se
verificó con el panel en estado operativo (33/33 componentes) y compras desde
los tres nodos.

| Resumen | Resultado |
|---|---|
| Concurrencia (45 compradores, 6 asientos, 3 nodos) | 6 ventas exactas, inventario consistente |
| Volver a vender tras apagar un nodo (RTO) | 28 a 51 s, sin intervención |
| Compras confirmadas perdidas (RPO) | 0 |
| Partición de red | La parte aislada pasa a solo lectura; nunca hay dos principales |
| Reincorporación de un nodo | 25 a 28 s, como réplica síncrona |

## 1. Failover automático de PostgreSQL (2 PC + árbitro)

Comando: `python3 tests/ha/laboratorio_patroni.py` (datos medidos: [evidencias/20261113_failover_patroni.json](evidencias/20261113_failover_patroni.json)). Usa la configuración que
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

## 5. Clúster completo de 3 máquinas levantado con `unir.sh` (16/11/2026)

Se simularon las 3 computadoras en un solo equipo, cada una con su propia IP
(10.99.0.11 pc1, 10.99.0.12 pc2, 10.99.0.13 árbitro) y su propia carpeta. Cada
"PC" se encendió con un solo comando: `bash unir.sh iniciar <paquete>.tgz`.

| Prueba | Resultado |
|---|---|
| Arranque con `unir.sh` | pc1 y pc2 con sus 10 servicios sanos; árbitro votando; administrador creado |
| Topología | usuarios y compras: principal pc1, réplica síncrona pc2; eventos: principal pc2, réplica síncrona pc1 |
| Directorio y Bully | 6 instancias vivas (3 por PC); coordinador compras_pc1 |
| Concurrencia entre las dos PC | 30 compradores repartidos en ambos gateways, 5 asientos: **5 vendidas** (2 por pc1, 3 por pc2), inventario consistente |
| **Apagado de golpe de la PC troncal (pc1, 10 contenedores)** | El sistema **volvió a vender por pc2 a los 42.7 s**, sin intervención. Las tres bases quedaron con su principal en pc2 |
| Reencendido de pc1 con `unir.sh iniciar` | pc1 vuelve como **réplica síncrona de las tres bases** y recupera el rol de coordinador (Bully) |

Durante esta prueba se corrigió un error previo de `nodos.py`: con una sola
copia de una base, la dirección de conexión no separaba el puerto del nombre.

## 6. Tres PC completas (sin árbitro) con `unir.sh` (17/11/2026)

Inventario `examples/7-tres-pc-patroni.yml`, 3 máquinas simuladas con IP propia,
imágenes reconstruidas desde el último commit. Cada PC se encendió con
`unir.sh iniciar`.

| Prueba | Resultado |
|---|---|
| Arranque | Las 3 PC con sus 10 servicios sanos; 9 instancias vivas en el directorio |
| Topología | Cada base con su principal en una PC distinta (usuarios pc1, eventos pc2, compras pc3), una réplica síncrona y una asíncrona |
| Concurrencia en 3 gateways | 45 compradores, 6 asientos: **6 vendidas**, inventario consistente |
| Apagado de golpe de pc1 | Se volvió a vender a los **28.2 s**; cada base conservó una réplica; pc1 reincorporada en 28 s |
| Apagado de golpe de pc3 | Se volvió a vender a los **41.1 s**; reincorporada en 25 s |
| Apagado de golpe de pc2, con las **tres** bases principales | Se volvió a vender a los **33.8 s**; las tres bases se promovieron en pc1; reincorporada en 25 s |
| Apagado de golpe de pc2 (versión final, 20/11/2026) | Se volvió a vender a los **50.5 s**; eventos_db pasó de pc2 a pc1 |
| Integridad tras las tres caídas | Inventario consistente en todos los eventos, sin ventas perdidas ni duplicadas; coordinador y 9 instancias de vuelta |

Se corrigió `unir.sh estado`, que en Fedora/Linux mostraba "sin respuesta"
aunque los servicios estaban sanos (ahora consulta también la IP de la PC).

## 7. Seguridad y procesos: actualización sobre un clúster con datos (19/11/2026)

Se encendieron las 3 PC simuladas con la versión anterior, se hicieron 6 compras
(30 compradores, 6 asientos) y luego se actualizó **una PC a la vez** con
`docker compose up -d usuarios eventos compras frontend`, como se hará en el laboratorio.

| Prueba | Resultado |
|---|---|
| Actualización PC por PC | Las 3 PC siguieron atendiendo durante la actualización; la base de Usuarios pasó sola a la revisión `20261119_03`; 31 cuentas y el inventario conservados |
| Bloqueo por intentos fallidos | 5 contraseñas erróneas **repartidas entre las 3 PC** bloquean la cuenta (el contador está en la base, no en cada PC); con la contraseña correcta responde 429 durante 120 s; el administrador la desbloquea desde el panel |
| MFA (TOTP) | Configurar muestra un QR; activado, el inicio de sesión pide el código de 6 dígitos sin emitir token; un código ya usado se rechaza (protección contra reenvío); desactivar requiere un código válido. Probado por API y en el navegador |
| Procesos | El Centro de Control muestra PID, estado, hilos, memoria, CPU, cambios de contexto y procesos del contenedor de las 9 instancias |
| Panel | 33/33 componentes sanos tras la actualización |

## 8. Pruebas automáticas

`python3 -m pytest -q`: 123 pruebas aprobadas (las suites con infraestructura se habilitan aparte; ver [PRUEBAS.md](PRUEBAS.md)).

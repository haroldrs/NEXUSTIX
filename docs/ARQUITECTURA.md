# Actualización de arquitectura — 3 de noviembre de 2026

La ampliación conserva las APIs anteriores y añade pedidos, pago simulado,
entradas QR, control de acceso, reembolsos, catálogo administrable y outbox.
Diagrama actual, contratos, máquina de estados e invariantes:
[EVOLUCION_DISTRIBUIDA](EVOLUCION_DISTRIBUIDA.md).
El entorno nuevo usa `docker-compose.distribuido.yml`, tres primarios y tres
standbys; la topología VM está en [despliegue-vm](../despliegue-vm/README.md).
Las limitaciones de `/compras/grupal` descritas abajo corresponden al contrato
legacy: el nuevo pedido grupal usa `/ordenes` con idempotencia durable.

La sección siguiente conserva la arquitectura legacy y sus ventanas de fallo.

---

# Arquitectura de NEXUSTIX

## 1. Componentes

```
                    navegador (http://localhost:8080)
                     │  config.js indica las URLs de las APIs
        ┌────────────┼──────────────────────┬──────────────────────┐
        ▼            ▼                      ▼                      ▼
   frontend      Usuarios :8001        Eventos :8002        compras_lb :8005 (Nginx)
   (Nginx)       login / JWT RS256     catálogo, stock,       round robin + failover
                 refresh rotatorio     reservas por           pasivo; reintenta UNA vez
                     │                 operacion_id           POST /compras (idempotente)
                     ▼                      ▲  ▲                 │            │
               usuarios_db                 │  │ X-Service-Token  ▼            ▼
                                           │  └──────────── compras1 :8003  compras2 :8004
                                      eventos_db                  │            │
                                                                  └─────┬──────┘
                                                                        ▼
                                                                   compras_db
                                                          (compartida: compras, claves
                                                           idempotentes, journal)
```

| Servicio | Imagen (demo / pruebas) | Responsabilidad | Depende de |
|---|---|---|---|
| `frontend` | `nexustix/frontend:local` / `nexustix-staging/frontend:auth` | HTML/JS estático; `config.js` sale del secreto `frontend_config` del entorno | — |
| `usuarios` | `nexustix/usuarios:local` | Registro (rol comprador), login Argon2id, JWT RS256 de 10 min, refresh HttpOnly rotatorio con detección de reutilización | `usuarios_db` |
| `eventos` | `nexustix/eventos:local` | Catálogo, stock (`stock`, `stock_inicial`), reservas idempotentes por `operacion_id` con `SELECT … FOR UPDATE` | `eventos_db` |
| `compras1`, `compras2` | `nexustix/compras:local` (misma imagen) | Valida el JWT **localmente** (clave pública), reclama la `Idempotency-Key`, ejecuta la saga con Eventos, historial, auditoría, recuperación | `compras_db`, Eventos (en tiempo de compra) |
| `compras_lb` | `nexustix/compras-lb:local` (Nginx 1.28) | Entrada única de Compras; re-resuelve DNS (`resolve`) | — (arranca aunque falten réplicas) |
| `*_db` | `nexustix/postgres:15` (PostgreSQL 15 oficial + envoltorio) | Una base por servicio, volumen persistente propio | — |

Puertos: demo publica 8001–8005 y 8080 en `127.0.0.1` (configurable con
`NEXUSTIX_BIND_ADDRESS`); pruebas publica 18080–18085 solo en loopback. Las
bases no se publican en el host.

Usuarios **no** participa en la compra: Compras verifica la firma del JWT con
la clave pública. Por eso una caída de Usuarios impide iniciar sesión o
renovar el token, pero no las compras con un token vigente.

## 2. Flujo real de una compra

1. El navegador carga el catálogo de Eventos (`GET /eventos`).
2. El usuario inicia sesión en Usuarios: recibe el access token (en memoria
   del navegador) y una cookie de refresco `HttpOnly` limitada a `/auth`.
3. El frontend genera una `Idempotency-Key` por intención de compra (la
   conserva si el resultado es incierto) y envía `POST /compras` al balanceador.
4. Nginx elige una réplica (round robin). Si la conexión falla, vence el
   tiempo o la réplica responde 502/503/504, reenvía **la misma petición** una
   vez a la otra réplica (`non_idempotent` solo en `location = /compras`).
5. La réplica valida el JWT y **reclama la clave** en `purchase_idempotency`
   (`state=processing`, `operacion_id` nuevo) con `COMMIT`; después toma
   `SELECT … FOR UPDATE` de esa fila (máx. 15 s; si otra réplica la tiene,
   responde 503 reintentable). Misma clave+sujeto+cuerpo ⇒ se reanuda o se
   devuelve la respuesta guardada; otro cuerpo ⇒ 409.
6. Reserva en Eventos `POST /eventos/{id}/reservar?operacion_id=…`: Eventos
   bloquea la fila del evento, descuenta stock (nunca < 0, `CHECK`) y registra
   la reserva `reservada`. Repetir la misma `operacion_id` no descuenta dos veces.
7. Compras inserta la compra y marca la clave `completed` con la respuesta,
   **en la misma transacción** (`confirmacion_reserva=pendiente`).
8. Confirma en Eventos (`/confirmar`) y actualiza la respuesta guardada a
   `confirmada`.
9. Responde 200 al cliente (con `atendido_por` y la cabecera
   `X-Nexustix-Upstream` del balanceador).

Si cualquier paso remoto no deja evidencia verificable, Compras consulta el
estado durable (`GET /eventos/reservas/{operacion_id}`) antes de decidir; si
sigue sin evidencia responde 503 `recuperacion_requerida` y **no** compensa a
ciegas.

## 3. Consistencia: saga, no transacción ACID distribuida

Compras y Eventos tienen bases distintas. **No existe una transacción ACID
global**; el protocolo es una saga con identificador de operación,
idempotencia en ambos lados y recuperación basada en evidencia de las dos bases.

| Ventana de fallo | Estado durable | Qué ocurre / cómo se resuelve |
|---|---|---|
| Antes de reclamar la clave | nada | El reintento empieza de cero. |
| Clave reclamada, sin reserva | clave `processing` | El reintento con la misma clave reanuda la misma `operacion_id`. La recuperación lo informa como `claim_sin_reserva_reintentable` y **no** lo cierra por antigüedad. |
| Reserva hecha, sin compra | clave `processing` + reserva `reservada` | Reintento: Eventos devuelve la reserva existente y la compra se completa. Si el cliente nunca vuelve, la recuperación **administrativa** libera la reserva (solo si la clave está vieja y sin dueño vivo, comprobado con `FOR UPDATE NOWAIT`) y guarda la respuesta 409 "compensada". |
| Compra guardada, reserva sin confirmar | compra + clave `completed` (pendiente) + reserva `reservada` | El replay devuelve la compra (`pendiente`). La recuperación (admin o automática) confirma la reserva y repara el replay. Nunca se libera una reserva con compra. |
| Todo hecho, respuesta perdida | todo confirmado | El replay devuelve la misma respuesta (`Idempotency-Replayed: true`). |

Invariantes verificadas por `GET /auditoria/consistencia` (admin):
`stock_inicial = stock_actual + vendidas` por evento y en total, ninguna
reserva pendiente, cada compra con su reserva confirmada (mismo evento y
cantidad) y viceversa. El journal (`journal_compras`) es una bitácora
funcional y **no** es fuente de verdad.

La compra **grupal** (`/compras/grupal`) no tiene idempotencia de extremo a
extremo; el balanceador nunca la reenvía y no debe reintentarse a ciegas.

## 4. Mecanismos de tolerancia a fallos (no son equivalentes)

| Mecanismo | Qué cubre | Implementación |
|---|---|---|
| Reinicio automático de contenedor | El proceso muere (excepción fatal, `os._exit`, OOM) | `restart: unless-stopped`; una parada manual (`stop`, `kill`) se respeta. |
| Healthcheck funcional | Distinguir "proceso vivo" de "servicio útil" | APIs: `/health` hace `SELECT 1`; PostgreSQL: TCP + consulta; Nginx: endpoint local. `depends_on: service_healthy` para el arranque. |
| Failover entre réplicas de Compras | Una réplica caída o colgada | Nginx: `max_fails=1 fail_timeout=10s`, `proxy_next_upstream` con un reintento, `resolve`. Pasivo: detecta la caída con la primera petición fallida. |
| Recuperación de operación distribuida | Sagas interrumpidas | Idempotencia en ambas bases + `/compras/recuperar-servidor` (admin) + barrido automático (una réplica a la vez por *advisory lock*; solo avanza compras persistidas). |
| Reconexión a PostgreSQL | Reinicio o promoción de la base | `pool_pre_ping`, `connect_timeout`, keepalives TCP de libpq y del servidor, `idle_in_transaction_session_timeout=120s`. |
| Alta disponibilidad de la base | Pérdida de un nodo PostgreSQL | Overlay de replicación: standby en caliente + promoción **manual controlada** con fencing; las APIs siguen al primario por URL multi-host `target_session_attrs=read-write`. Ver `BASES_DE_DATOS.md`. |
| Recuperación del equipo completo | Reinicio del host | Volúmenes persistentes + `unless-stopped` (los contenedores que estaban en marcha vuelven). **No** hay tolerancia a la pérdida del host: todo corre en una PC (ver `ARQUITECTURA_DOS_PC.md`). |

Puntos únicos de fallo en el despliegue de una PC: Usuarios, Eventos, el
balanceador, el frontend, cada base sin replicación y el propio host.

## 5. Seguridad

- JWT RS256 (`iss`, `aud`, `exp`, `jti` obligatorios); solo Usuarios monta la
  clave privada. Roles `comprador` (registro público) y `administrador`
  (creado con `bootstrap_admin.py`, nunca al arrancar).
- Refresh: cookie `HttpOnly`, `SameSite=Strict`, `Path=/auth`, rotación por
  uso y revocación de toda la familia si se reutiliza un token revocado;
  `Origin` permitido + cabecera `X-CSRF-Protection: 1`. `Secure` se activa con
  `NEXUSTIX_AUTH_COOKIE_SECURE=true` (requiere HTTPS).
- Compras↔Eventos: `X-Service-Token` comparado en tiempo constante.
- Auditoría, journal, recuperación y simulaciones: solo administrador.
- Ganchos de caída para pruebas: inertes salvo `NEXUSTIX_DEPLOYMENT_ENV=staging`
  **y** `NEXUSTIX_TEST_CRASH_HOOKS=1` (solo el Compose de pruebas los define).
- Secretos: generados localmente fuera del repositorio (`config/README.md`).

## 6. Observabilidad

`/health` (con estado de la recuperación automática en Compras), `/metricas`
por réplica, `/compras/journal`, `/auditoria/consistencia`, cabecera
`X-Nexustix-Upstream`, `python3 scripts/nx.py <entorno> status` y
`python3 scripts/db_ha.py <entorno> status`.

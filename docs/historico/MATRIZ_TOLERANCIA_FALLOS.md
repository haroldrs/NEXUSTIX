# Matriz global de tolerancia a fallos NEXUSTIX

**Revisión:** 2026-10-24. Matriz basada en `docker-compose.yml`,
`docker-compose.staging.yml`, handlers actuales y evidencia anotada en
`AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md`. Las entradas de staging no equivalen
a una garantía de producción. El despliegue actual ocupa un solo host.

## Arquitectura desplegada

```text
Browser -> frontend Nginx -> Usuarios :8001 -> usuarios_db
                         -> Eventos :8002 -> eventos_db
                         -> compras_lb :8005 -> Compras1 :8003 ┐
                                              -> Compras2 :8004 ┴-> compras_db
Compras -> Eventos: reservas/confirmaciones/releases por HTTP
Todas las bases y aplicaciones -> mismo host Docker y red local
```

Compras valida localmente la firma RS256 del JWT. Compras no consulta Usuarios
en cada compra. Compras1/2 comparten PostgreSQL de Compras; Eventos mantiene su
propio stock y tabla de reservas. Las bases no tienen réplicas declaradas.

## Matriz

| Componente que falla | Impacto y servicios que siguen | Detección | Recuperación actual y automatización | Evidencia / comprobación necesaria |
|---|---|---|---|---|
| Frontend Nginx | La UI queda inaccesible; las APIs todavía pueden responder directamente. El usuario no cuenta con una ruta pública alternativa documentada. | Navegador/HTTP de `:8080` o `:18080`; no hay monitor externo. | `restart: unless-stopped`; reinicio de contenedor manual/por Docker. No hay segunda UI ni failover de entrada. | No hay prueba de caída del frontend. |
| Usuarios API | Registro, login, refresh y logout fallan. Compras existentes con JWT aún válido pueden continuar porque Compras verifica firma/expiración localmente y no consulta Usuarios por compra. No se admiten compras anónimas. Revocación/desactivación no invalida inmediatamente access tokens ya emitidos. | `/health` (desde este cambio, 503 si falla SQL); llamadas HTTP de auth. | Una instancia; Docker reinicia según Compose. Requiere Usuarios DB y clave privada disponible. No hay réplica de app ni failover. | Pruebas de auth aisladas y staging aprobadas históricamente; caída real de Usuarios no probada en esta fase. |
| Eventos API | Catálogo/stock y toda compra que requiera reservar/confirmar quedan indisponibles o ambiguos. Si una reserva fue persistida y se perdió la respuesta, se debe consultar su estado por `operacion_id`; no liberar ni confirmar a ciegas. | `/health`, timeout/HTTP 5xx en Compras, consultas internas de estado. | Una instancia; `unless-stopped`. Recovery de Compras es una ruta administrativa manual, no worker. | Staging probó Eventos detenido antes de reserva y respuesta perdida con barrera; caída abrupta/DB no probadas. Ver sección de auditoría. |
| Compras1 | URL directa `:8003` falla; el balanceador puede usar Compras2 si Compras2, Nginx, DB y Eventos sirven. | Health de instancia, errores pasivos de Nginx y HTTP. | `on-failure:5`; reinicio de proceso automático limitado. Failover ordenado y cuatro checkpoints abruptos tuvieron evidencia staging para Compras1. | No demuestra fallo de DB, host ni balanceador. POST enviado no se reintenta automáticamente por Nginx tras error de upstream no idempotente; el cliente debe reusar el mismo `Idempotency-Key`. |
| Compras2 | URL directa `:8004` falla; Compras1 puede seguir detrás de Nginx. | Igual a Compras1; health incluye `instancia=2`. | `on-failure:5`. Parada ordenada aprobada; los cuatro escenarios abruptos siguen pendientes porque el estado observado fue inconcluso. | No repetir abrupt crash hasta tener instrumentación que preserve el ID/metadatos del contenedor fallido. |
| Balanceador de Compras Nginx | El frontend que usa `:8005` no puede comprar; APIs directas de Compras pueden continuar para diagnóstico, pero no constituyen entrada redundante soportada. | Health endpoint a través de `:8005`; inspección del contenedor. No hay healthcheck activo del upstream. | Una instancia `unless-stopped`, sin segundo LB/VIP. El reintento pasivo cubre errores/timeout/502/503/504; `non_idempotent` no está habilitado. | Failover de cada réplica individual probado con parada ordenada. Caída del LB no probada. |
| `usuarios_db` | Registro/login/refresh no sirven; compras con JWT no expirado pueden seguir en lo que respecta a identidad, pero las otras dependencias de la compra deben estar disponibles. | PostgreSQL healthcheck y `/health` de Usuarios. | Contenedor único con volumen. Reinicio del contenedor; recuperación desde backup manual. Sin réplica/PITR. | No hay caída real de DB en staging. |
| `eventos_db` | Catálogo y stock/reservas no verificables. Compras debe fallar/retener operación ambigua, no completar por suposición. | PostgreSQL healthcheck, `/health` Eventos, errores de Compras. | Contenedor único con volumen; restart/restore manual. Sin réplica/PITR. | No se probó indisponibilidad de la DB. |
| `compras_db` | Claim, replay, compras e historial no están disponibles. No es seguro aceptar una compra nueva porque no se puede garantizar idempotencia. Eventos puede haber reservado stock y quedar estado ambiguo. | PostgreSQL healthcheck y `/health` de ambas réplicas; 5xx/timeout. | Contenedor único con volumen. Reinicio/restore manual y conciliación con Eventos antes de reabrir compras. | No se probó caída de esta DB. |
| Host Docker 1 (actual) | Caen frontend, APIs, balanceador y las tres DB; no queda capacidad local para comprar, autenticar o validar stock. | Fuera de banda (SSH/monitor del host); no se declara monitor externo. | No hay failover de host. Restaurar en otro host desde copia externa y ejecutar reconciliación. | No hay prueba de pérdida del host. |
| Host Docker 2 | No existe como nodo NEXUSTIX desplegado. | No aplica. | No hay réplica ni mecanismo de conmutación entre hosts. | El directorio `multi-laptop` es configuración heredada, pre-auth y no aprobada para operar. |
| Red entre hosts | No aplica al despliegue actual de un host; en dos hosts separaría dependencias y puede dejar servicios sin sus bases o APIs. | Probes de red y healthchecks de aplicación aún no están desplegados. | Timeouts HTTP y reintentos del cliente con misma key; no hay cola durable ni failover de red. | Plan de pruebas etapa 8–10; no probado. |

## Degradación de Usuarios y JWT

- Login/registro/refresh dependen de Usuarios y `usuarios_db`.
- Compras verifica JWT localmente con clave pública. Un token válido antes de la
  caída de Usuarios puede seguir usándose hasta `exp`; el TTL normal documentado
  es 600 s. Esto no omite autenticación ni permite identidad anónima.
- Logout revoca el refresh token en Usuarios y elimina su cookie; un access
  token emitido sigue válido hasta expirar. No existe introspección/revocación
  online por compra.
- Para redundar Usuarios hacen falta al menos dos procesos con la misma clave
  pública, privada solo en firmantes, configuración homogénea y una DB única
  compartida o replicada. Duplicar el proceso sin redundar `usuarios_db` no
  elimina la dependencia única.

## Distinción de escenarios

- `docker compose stop`: parada ordenada solicitada; Docker no la reinicia por
  la política de salida. Staging validó este caso para las dos réplicas.
- `os._exit(86)` del hook: salida abrupta de PID 1 en puntos de compra; hook
  inerte por defecto y restringido a `staging`. Compras1 tiene cuatro casos
  aprobados; Compras2 sigue pendiente/inconcluso.
- Reinicio de contenedor: mismo contenedor y volúmenes; el `RestartCount` debe
  distinguirlo de una recreación.
- `up --force-recreate`: reemplaza contenedor; no prueba por sí solo un crash y
  destruye los metadatos/logs locales del contenedor reemplazado.
- Caída de Nginx, DB o host: distinta de caída de una réplica y no cubierta por
  las pruebas de failover de Compras.

## Reconciliación antes de reabrir compras

1. Bloquear temporalmente nuevas compras y registrar el instante/copia usada.
2. Comparar por `operacion_id`: claims, compras, reservas (estado/cantidad/evento),
   stock y journal. El journal es best-effort, no verdad primaria.
3. Mantener pendientes los estados sin evidencia; no restablecer stock usando
   solo `EVENTOS_BASE` si hubo cambios de inventario legítimos.
4. Ejecutar el endpoint de recuperación solo con autorización admin y después
   de inspeccionar candidatos exactos. Repetir la auditoría de solo lectura.
5. Reabrir compras solo cuando las diferencias estén explicadas y la ecuación de
   stock para el alcance verificado coincida.

La suite de staging da evidencia para algunas carreras y operaciones sintéticas,
pero no para todos los escenarios de esta matriz. No existe alta disponibilidad
integral ni garantía de cero pérdida.

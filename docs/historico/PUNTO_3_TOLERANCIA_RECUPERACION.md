# Punto 3 — Tolerancia a fallos y recuperación

## Objetivo

Demostrar la disponibilidad ante la caída de una réplica, el aborto de una transacción distribuida y la indisponibilidad de una dependencia. Antes y después de cada prueba se revisan disponibilidad, stock e historial.

## Mecanismos implementados

- **Réplicas y failover:** `compras1` y `compras2` comparten `compras_db`; Nginx distribuye las solicitudes y reintenta ante errores de conexión y determinados errores HTTP.
- **Persistencia de negocio:** las compras se almacenan en PostgreSQL y las reservas, confirmaciones y liberaciones se guardan en `eventos_db`. Los volúmenes Docker conservan los datos al recrear contenedores, siempre que no se eliminen los volúmenes.
- **Idempotencia:** `operacion_id` enlaza compra y reserva. Los endpoints de reservar, confirmar y liberar evitan aplicar dos veces la misma operación.
- **Recuperación explícita:** `POST /compras/recuperar-servidor` consulta las reservas pendientes. Si encuentra una compra persistida, confirma la reserva; si no encuentra compra, libera la reserva. Si Eventos no responde, declara `recuperacion_incompleta` y no afirma que la integridad esté verificada.
- **Auditoría:** `GET /auditoria/consistencia` compara stock inicial, stock actual, cantidades vendidas, reservas pendientes y correlación por `operacion_id`.
- **Bitácora persistente:** `journal_compras` almacena los eventos funcionales en `compras_db`, compartida por ambas réplicas. No sustituye el WAL nativo de PostgreSQL ni constituye por sí sola un protocolo de consenso.

## Preparación

Desde la carpeta raíz `Project/`:

```bash
docker compose up -d --build
docker compose ps
curl -s http://localhost:8005/auditoria/consistencia
```

La auditoría inicial debe indicar `todo_consistente: true`, `balance_global_diferencia: 0` y `reservas_pendientes: 0`. Si hay datos de pruebas anteriores, no restablecerlos a ciegas: guardar primero las evidencias. El script de pruebas no elimina volúmenes ni reinicia el historial de compras.

## Pruebas automatizadas

Instalar `requests` en el entorno local si hace falta (`python -m pip install requests`) y ejecutar:

```bash
python scripts/prueba_punto3_tolerancia.py
```

El script:

1. Guarda una auditoría, detiene `compras1`, comprueba `compras2` y el balanceador, compara los totales y restaura `compras1`.
2. Inyecta un fallo en una compra grupal antes del commit final, verifica `GLOBAL_ABORT` y comprueba que las compensaciones preservan stock e historial.
3. Detiene `usuarios`, intenta una compra, confirma el rechazo y restaura el servicio; después repite el proceso con `eventos`.
4. Consulta la bitácora desde ambas réplicas para verificar que ven el mismo historial persistente.
5. Ejecuta la auditoría final.

El script detiene y arranca contenedores, por lo que debe ejecutarse desde la raíz del proyecto con Docker Compose disponible. Si una prueba se interrumpe, revisar `docker compose ps` y levantar los servicios detenidos con `docker compose up -d`.

## Prueba manual de recuperación

```bash
# 1. Estado previo
curl -s http://localhost:8005/auditoria/consistencia
curl -s http://localhost:8005/compras/journal

# 2. Provocar una transacción grupal que aborte y compense el primer elemento
curl -i -X POST http://localhost:8005/compras/grupal \
  -H 'Content-Type: application/json' \
  -d '{"usuario_id":1,"items":[{"evento_id":1,"cantidad":1},{"evento_id":3,"cantidad":1}],"tipo_imprevisto":"apagon_maquina"}'

# 3. Ejecutar conciliación y volver a auditar
curl -s -X POST http://localhost:8005/compras/recuperar-servidor
curl -s http://localhost:8005/auditoria/consistencia
```

## Diferencia importante: fallo simulado y caída real

`tipo_imprevisto: "apagon_maquina"` en el endpoint grupal inyecta una excepción controlada en el flujo de la transacción; **no equivale a matar abruptamente el proceso o cortar físicamente la máquina**. Para documentar una caída real del contenedor, puede detenerse `compras1` con `docker compose stop compras1` y comprobar el failover. Esa prueba demuestra disponibilidad de la réplica sobreviviente, pero no garantiza que se haya interrumpido una transacción en una ventana crítica.

Para una prueba destructiva de caída durante una operación real, usar un entorno de laboratorio y una operación identificada, registrar la auditoría antes, detener el contenedor en el momento controlado y ejecutar la recuperación después. No hacerlo contra datos que se necesiten conservar.

## Evidencias que deben adjuntarse al informe

Para cada prueba guardar: comando ejecutado, código HTTP, respuesta JSON, `docker compose ps`, stock del evento involucrado, historial de compras y resultado completo de `/auditoria/consistencia` antes y después. Una prueba solo se considera satisfactoria si la recuperación termina y la auditoría confirma la consistencia; un `HTTP 200` aislado no es suficiente.

## Límites conocidos

- El estado del algoritmo Bully expuesto por `/lider/*` es una simulación operativa por instancia, no una elección distribuida resistente a particiones: sus variables están en memoria y no existe consenso entre nodos.
- El reloj de Lamport ayuda a ordenar eventos observados por cada servicio, pero al estar en memoria se reinicia con el proceso y no reemplaza un identificador único ni un registro persistente.
- `journal_compras` es una bitácora funcional, no un sistema de replicación de PostgreSQL ni una garantía de atomicidad entre dos bases independientes. La reconciliación y la auditoría detectan/reparan estados pendientes según el registro disponible.

# Punto 1 — Validación de datos y consistencia

## Objetivo

Evitar cantidades inválidas, impedir stock negativo, hacer idempotentes las reservas y compensaciones, y auditar el stock frente al historial de compras.

## Cambios incluidos

- `CompraRequest` exige identificadores positivos y `cantidad` entre 1 y 1000.
- Los ítems de una compra grupal aplican las mismas restricciones; el lote admite entre 1 y 20 elementos.
- Eventos valida los parámetros de reserva/liberación y mantiene restricciones de base de datos para `stock >= 0` y `cantidad > 0`.
- Cada reserva usa `operacion_id`, registrada en `reservas_stock`; repetir una reserva con la misma clave no vuelve a descontar entradas.
- La compensación exige la misma operación y cantidad, solo libera reservas aún no confirmadas y no puede incrementar dos veces el stock.
- La compra guarda `operacion_id` para correlacionar su persistencia con la reserva en Eventos.
- La auditoría consulta el stock inicial y actual desde Eventos. Si Eventos no responde, devuelve HTTP 503 en vez de afirmar que el sistema es consistente con datos de respaldo codificados en Compras.
- La auditoría detecta reservas pendientes, compras que apuntan a eventos inexistentes y operaciones nuevas sin correspondencia entre compra y reserva confirmada.
- `/compras/recuperar-servidor` intenta reconciliar reservas pendientes: confirma las que tienen una compra persistida y libera las que no tienen compra confirmada. Luego ejecuta la auditoría real.
- `POST /eventos/reset` solo restablece el stock; no borra el historial. Para una prueba totalmente limpia se proporciona un script destructivo que requiere confirmación explícita.

## Ejecución

Desde la carpeta `Project/`:

```bash
docker compose up -d --build
docker compose ps
python scripts/prueba_punto1_consistencia.py
```

La prueba comprueba que cantidades 0 y negativas se rechacen con HTTP 422, que repetir una reserva no descuente stock dos veces y que repetir una compensación tampoco incremente el stock dos veces. Finalmente muestra el resultado real de la auditoría.

Para exigir que el estado final esté consistente:

```bash
python scripts/prueba_punto1_consistencia.py --require-consistent
```

Si existen compras anteriores que se guardaron antes de añadir `operacion_id`, se informarán como registros antiguos. Se comparan mediante la ecuación global de stock, pero no pueden correlacionarse individualmente con una reserva.

## Restablecer el entorno de pruebas

**Destructivo:** elimina el historial de compras y reservas, y restablece el stock de los cinco eventos. No elimina los volúmenes de las bases de datos.

```bash
./scripts/reset_entorno_pruebas.sh
```

El script pide escribir `RESTABLECER` antes de realizar cambios. No usar si se deben conservar datos o capturas previas.

## Evidencias recomendadas

1. Compra con `cantidad: 0` rechazada por HTTP 422.
2. Reserva repetida con el mismo `operacion_id`, mostrando `idempotente: true` y stock sin cambios en el segundo intento.
3. Compensación repetida mostrando que el stock vuelve al valor previo una sola vez.
4. Respuesta completa de `/auditoria/consistencia` antes y después de una compra válida.
5. Respuesta de `/compras/recuperar-servidor` con número de reservas reconciliadas, errores y resultado de auditoría.

## Límites que deben documentarse

- La ecuación de stock presupone que el stock inicial de referencia corresponde al conjunto de datos que se está auditando. Si se restablece el stock sin limpiar o conciliar las compras, la auditoría debe señalar una discrepancia.
- Los registros de compras anteriores sin `operacion_id` no pueden correlacionarse operación por operación; se informan como históricos.
- Esta mejora no convierte las dos bases de datos en una única transacción ACID. La reserva y la compra siguen siendo una operación distribuida con compensación y reconciliación.

La bitácora expuesta en `/compras/journal` continúa siendo un registro reciente en memoria de cada réplica. No es el WAL persistente de PostgreSQL y se pierde al reiniciar el proceso; el frontend ahora lo etiqueta correctamente.

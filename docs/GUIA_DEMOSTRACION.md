# Demostración del checkout distribuido — Fedora

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

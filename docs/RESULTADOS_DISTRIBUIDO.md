# Resultados reales — Fedora, 3 de noviembre de 2026

Laboratorio `nexustix-distributed-lab`, disponible en **http://localhost:28080**.
Se mantuvo operativo y separado `nexustix-auth-staging`: la suite HTTP comprobó
igualdad de IDs y tiempos de arranque antes/después de las caídas del laboratorio.
La inspección final encontró 12 contenedores saludables en laboratorio y 13 en
staging. No se borraron ni reinicializaron volúmenes existentes. No se ejecutaron
VMs, cambios de red del host ni configuración de firewall.

## Ejecuciones y evidencia

| Ejecución UTC | Entorno / comando | Resultado |
|---|---|---|
| `20261103T114909Z` | `.venv/bin/python scripts/ejecutar_pruebas.py --capas unit` | 105 pruebas Python + 1 prueba Node aprobadas; 0 fallidas |
| `distribuido-20261103T114909Z` | `.venv/bin/python scripts/probar_local.py` | 24 pruebas PostgreSQL 18.6 aprobadas; 0 fallidas |
| `distribuido-20261103T115023Z` | `scripts/probar_local.py tests/postgres/test_checkout.py` | 12 aprobadas tras el ajuste final de autorización; son una repetición del subconjunto, no 12 casos nuevos |
| `http-distribuido-20261103T114937Z` | `scripts/verificar_distribuido.py … --fallos` | 8 escenarios aprobados por HTTP/Docker PostgreSQL 15.19 |
| `http-distribuido-20261103T115152Z` | `scripts/verificar_distribuido.py …` | 6 escenarios sin detenciones aprobados tras actualizar Eventos |
| Configuración | `docker compose … config --quiet` | Laboratorio y tres manifiestos VM válidos; no acredita despliegue VM |
| Frontend | `node --check` y Firefox headless con perfil temporal | Sintaxis correcta; portal y catálogo real renderizados. Flujo interactivo completo pendiente |

Las salidas completas y XML están en `resultados/<ejecución>/`. El resumen
portable de casos, estados y hashes está en
[evidencias/20261103_distribuido.json](evidencias/20261103_distribuido.json).
Captura final del portal en Firefox: [20261103_portal_fedora.png](evidencias/20261103_portal_fedora.png).
No incluye sesión ni credenciales. Los Compose de demo y staging se compararon
con el respaldo inicial: siguen idénticos; no hay archivos eliminados respecto
a ese respaldo.

La advertencia de 3 capas PENDIENTES en el informe unitario significa que ese
comando solo ejecutó `unit`; PostgreSQL se ejecutó por separado. No se lanzaron
las suites históricas de fallos que intervienen staging.

Las advertencias Python corresponden principalmente a APIs de inicio de FastAPI
obsoletas y fechas UTC sin zona en código heredado; no se ocultaron los contadores
(1374 en la suite PostgreSQL completa). No se presentan como errores corregidos.

## Matriz de los 15 escenarios solicitados

APROBADA se refiere exclusivamente al entorno y mecanismo descritos. Las pruebas
locales de PostgreSQL usan procesos reales, bases nuevas y puertos aleatorios;
el transporte entre APIs de esa suite usa TestClient. La suite HTTP del laboratorio
usa peticiones reales a los contenedores y Nginx.

| # / Escenario | Estado inicial | Acción | Esperado | Obtenido / estado |
|---|---|---|---|---|
| 1. Compra individual | Evento con stock, comprador autenticado | Reservar y aprobar pago simulado | Pedido completo, compras y QR únicos | APROBADA: pedido completo y entradas persistidas; PostgreSQL + HTTP |
| 2. Compra grupal | Dos eventos disponibles | Un pedido con varios ítems y pago | Todos los ítems confirmados, importe del servidor | APROBADA: 5 entradas para 2 ítems en PostgreSQL; flujo HTTP completo |
| 3. Última entrada | Un asiento disponible, dos compradores | Pedidos concurrentes | Un ganador, sin sobreventa | APROBADA: una reservada y otra cancelada; PG con usuarios distintos, HTTP con dos intenciones simultáneas |
| 4. Idempotencia | Misma cuenta, cuerpo y clave | Enviar simultáneamente; repetir pago | Una orden/pago; cambio de cuerpo rechazado | APROBADA: una orden y pago, cuerpo/usuario distinto 409; PG + HTTP |
| 5. Pago rechazado | Reserva activa | Rechazar y repetir rechazo | Cancelar y devolver inventario una vez | APROBADA: cancelada, inventario original; PG + HTTP |
| 6. Caída de Compras | Dos réplicas saludables | Detener Compras1 del laboratorio; comprar por Nginx | Compras2 responde y completa pedido | APROBADA: health indica instancia 2 y compra completa; Compras1 reanudada |
| 7. Caída de VM app | Topología preparada, sin VMs operativas | No ejecutada por la restricción de esta fase | Medir indisponibilidad y recuperación de VM | PENDIENTE: ninguna caída de VM simulada; una única VM app sigue siendo punto único de fallo |
| 8. Pérdida de conexión al primario | Pool Compras conectado al primario local | Detener PostgreSQL con modo immediate y promover standby | Descartar conexión vieja y retomar compras | APROBADA en procesos PG locales: mismo pool reconecta; partición de red entre VMs PENDIENTE |
| 9. Promoción de standby | Standby real replicado, primario detenido | Ejecutar SQL de promoción del procedimiento | Un nodo escribible; compras previas conservadas | APROBADA en PG local: compra tras promoción y 3 compras conservadas; fencing Docker/remoto no ejercitado |
| 10. Reintegrar primario | Antiguo nodo detenido y nuevo primario escribible | Conservar directorio antiguo, clonar a nuevo con pg_basebackup | Antiguo nodo solo lectura y datos replicados | APROBADA: recovery=true, 3 compras, escritura rechazada; prueba local, sin tocar volúmenes existentes |
| 11. Backup + WAL | Base con fila 1, backup verificado | Insertar 2, marcar punto, insertar 3; restaurar en directorio nuevo | Recuperar 1 y 2, excluir 3 | APROBADA: filas 1/2 y escritura nueva 4 tras promoción; destino existente rechazado |
| 12. Reinicio con pendientes | Pedido reservado persistido | Detener ambas Compras, vencer solo ese pedido sintético, arrancar | Worker retoma y compensa | APROBADA por HTTP: cancelación automática y stock consistente; además emisión interrumpida se retoma en PG sin duplicar compra |
| 13. Repetición de eventos | Outbox publicado y notificación persistida | Marcar entrega pendiente y repetir barrido | No duplicar efecto en buzón | APROBADA en PG: mismo número de notificaciones, deduplicación por ID |
| 14. QR duplicado | Entrada emitida sin uso | Validar dos veces/concurrentemente | Solo un acceso permitido | APROBADA: 200 y 409; comprador no puede validar; PG + HTTP |
| 15. Inventario final | Operaciones de venta, rechazo, cancelación y reembolso | Auditar ecuación y disponibilidad del asiento | Inicial = disponible + reservado + confirmado | APROBADA: invariantes tras flujos y stock restituido una sola vez; PG + HTTP |

Cobertura adicional: autorización del organizador y auditoría de catálogo,
privacidad de pedidos, expiración sin cobro, emisión fallida recuperable, respuesta
de reserva perdida, reembolso que revoca QR, aislamiento frente al recuperador
legacy y rechazo de reservas tardías después de cancelar un evento inexistente.

## Fallos observados y correcciones

- El rol de replicación del laboratorio se creó inicialmente sin contraseña:
  el proceso postgres no podía leer el secreto 0600. Se corrigió la lectura antes
  del cambio de usuario y la propagación de errores; se reparó el rol del nuevo
  laboratorio sin reinicializar datos. La repetición verificó los tres streamings.
- Una corrida HTTP se lanzó antes de terminar de conectar los standbys; falló
  esa aserción y se conserva en `http-distribuido-20261103T113902Z`.
- `distribuido-20261103T114154Z`: 22 aprobadas y 1 FALLIDA. La prueba PITR asumía
  que aceptar conexiones implicaba haber terminado la promoción; PostgreSQL
  podía aceptar lecturas aún en recuperación. Ahora espera promoción con límite
  de 20 s y comprueba filas y escritura posterior. Suite completa posterior: 24/24.
- Ajustes anteriores de heads de migración, bootstrap y fixtures de tablas nuevas
  se describen en [EVOLUCION_DISTRIBUIDA.md](EVOLUCION_DISTRIBUIDA.md).
- La inspección visual detectó que estilos de botones sobreescribían `hidden`;
  se añadió una regla explícita para respetar la visibilidad de sesión y paneles.

No se borraron resultados fallidos. Tampoco se consideran aprobadas pruebas de
VM, red privada, backup programado, fencing remoto o tolerancia a pérdida del
host. El procedimiento futuro está en [despliegue-vm](../despliegue-vm/README.md)
y los límites operativos en [LIMITACIONES.md](LIMITACIONES.md).

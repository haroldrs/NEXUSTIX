# Portal y operaciones: cambios preparados

Cambios de código únicamente: no se ejecutaron pruebas, migraciones, consultas
a bases reales ni operaciones de contenedores. Los despliegues existentes no
se modifican para activar estas funciones.

## Funcionalidades

- Catálogo con búsqueda, filtros, orden y carga progresiva. Cada evento abre
  `#evento/ID`: información y un único formulario, sin el bloque redundante.
- Precios generales en céntimos y precios por asiento conservados. El precio
  inicial usa asientos disponibles cuando existen; el total definitivo se
  fija al reservar, no a partir del cálculo del navegador.
- Selección, revisión y resultado; carrito de varios eventos; login al
  confirmar; claves idempotentes conservadas para reintentar; historial,
  pago simulado, reembolso y QR en **Mis entradas**.
- Administración unificada con permisos comprobados en backend: eventos,
  ventas, pedidos, entradas, reembolsos, usuarios y rol organizador.
- Mapa de dependencias, salud, métricas por proceso, recorrido de operaciones
  y diagnóstico PostgreSQL de solo lectura (rol, replicación, WAL y checkpoints).
- Laboratorio A–J con instrucciones manuales, capturas inicial/final,
  incidentes persistentes y reportes JSON/CSV/imprimibles. Se pueden retomar
  capturas iniciales desde Evidencias y exportar una consulta sin persistirla.

## Esquema pendiente de aplicar por el operador

Compras requiere la cadena Alembic `20261105_04` → `20261105_05` → `20261105_06`:

- `04`: solicitudes persistentes de cancelación.
- `05`: evidencias, incidentes y última observación por componente.
- `06`: trabajos de compensación por pedido/evento e importe reembolsado
  acumulado. Solo reconoce importes históricos verificables; no inventa precios.

La imagen de Compras ya utiliza `python -m shared.run_migrations` antes de
iniciar el servicio. Cuando autorices una actualización, ese mecanismo aplicará
las revisiones pendientes. `NEXUSTIX_AUTO_MIGRATE=0` evita esa ejecución, pero
la aplicación actualizada exige el esquema `20261105_06`. No se modificaron
credenciales ni la política existente de migraciones. Respaldar antes de aplicar;
las migraciones nuevas bloquean el downgrade para conservar el historial.

Eventos mantiene su mecanismo aditivo de arranque en `shared/catalog.py`:
añade metadatos y responsable de cancelación, y desacopla la auditoría del
borrado físico de eventos sin referencias. No modifica precios ni inventario
de eventos existentes. Este mecanismo se ejecutará cuando decidas iniciar
el código actualizado; no se ha ejecutado durante esta implementación.

Actualizar posteriormente las imágenes de frontend y servicios de forma
coordinada. No mezclar réplicas viejas y nuevas durante compensaciones.

## Configuración del observador

`NEXUSTIX_OPERATIONS_TOPOLOGY` admite un objeto JSON **en el entorno de ambas
instancias de Compras**. No es configuración del navegador ni un secreto.
Exportarlo en una terminal no lo inyecta automáticamente en los contenedores.

Plantilla sin activar: [topologia-operaciones.lan.example.json](topologia-operaciones.lan.example.json).
Sustituir `pc1.invalid`, `pc2.invalid` y `pc3.invalid` por destinos reales
alcanzables desde Compras, conservando los puertos del despliegue elegido.
Para un componente inexistente, usar `{"url": null}`. No incluir credenciales
en las URL. Los destinos solo proceden del servidor; no se aceptan desde el panel.

Sin configuración se consultan convenciones Docker locales, etiquetadas como
no verificadas; Usuarios B y Eventos B quedan desconocidos. La plantilla LAN
no configura contenedores, balanceadores, redes ni replicación. Los nodos de
PostgreSQL son etiquetas: su diagnóstico se obtiene por las API conectadas.

El frontend detecta `/checkout/capacidades`. Una opción explícita
`CONFIG.CHECKOUT_ORDERS=false` conserva la compra directa legacy; `true` exige
pedidos. Sin esa opción, un 404 conserva el modo legacy y una caída de red
bloquea el envío hasta poder consultar el modo.

## Límites importantes

- Cancelar cierra ventas y conserva una tarea durable. Antes del pago se
  libera todo el pedido grupal; después del pago se compensa únicamente el
  evento cancelado. Las entradas afectadas quedan inutilizables; los otros
  eventos pagados conservan sus entradas. Los reintentos reutilizan las mismas
  operaciones de inventario y el mismo registro de reembolso.
- Ventas/reservas legacy sin pedido y pago verificable quedan visibles como
  `legacy_pendientes`: requieren conciliación manual. No se inventa un
  reembolso ni se declara completada la cancelación. Un fallo de dependencia
  conserva trabajo pendiente, no equivale a rollback distribuido.
- Las métricas HTTP reinician con cada proceso. No existe un contador durable
  global de reintentos ni comprobante de entrega de respuesta al navegador.
- Los incidentes se detectan entre capturas explícitas, no mediante vigilancia
  continua. Sus tiempos son ventanas de observación, no RTO exacto. Una falta
  de respuesta no prueba una PC apagada. La instancia continuadora solo se
  informa si una lectura por el balanceador coincide con una réplica saludable.
- En la LAN antigua (`despliegue-pc`) las tres bases permanecen en PC2, sin replicación entre PC. No hay
  promoción automática ni afirmaciones de failover sin evidencia. Las consultas
  PostgreSQL opcionales pueden aparecer no disponibles por versión o permisos.
- Capturas e incidentes dependen de `compras_db`. Si Compras aún puede responder,
  permite exportar una consulta sin guardarla. Importaciones se marcan como no
  verificadas y no alteran salud ni incidentes. Se descartan campos no tipados.
- `/metricas` exige administrador. El journal omite detalles privados y su
  endpoint de limpieza responde 409: no hay procedencia sintética verificable.
  No se agregó limpieza automática ni controles de infraestructura al navegador.

## Comprobación manual posterior

La nueva ruta [despliegue por inventario](../despliegue-nodos/README.md) monta
`NEXUSTIX_OPERATIONS_INVENTORY_FILE` y reemplaza en ese entorno las etiquetas
PC1/PC2/PC3 por nodos declarados, observaciones HTTP y canales WAL. El laboratorio
muestra proyecciones estáticas de pérdida por nodo/dominio físico y accesos
alternativos; no ejecuta fallos ni promociones. Sin esa variable conserva la
configuración anterior. HA preparada no significa HA física comprobada; no se
ejecutaron pruebas o despliegues de esta ruta.

1. Catálogo, filtros, precios, detalle único y uso en móvil.
2. Login al confirmar, selección de asientos, pedido individual/grupal, pago,
   expiración, reintento con la misma clave y ausencia de duplicados.
3. Permisos: comprador sin acceso a administración, métricas ni evidencias.
4. Cancelación con ventas, reembolso parcial de grupo pagado, invalidación de QR,
   repetición de cancelación y recuperación tras interrupción autorizada.
5. Inventario y pagos consistentes; eventos con referencias no se eliminan.
6. Topología real, instancias observadas, diagnóstico PostgreSQL y estados
   desconocidos cuando una consulta no se puede verificar.
7. Capturas A–J, incidentes, recuperación, retomado de evidencia, exportación e
   importación; sin marcar un escenario exitoso únicamente por un healthcheck.

# Matriz de cumplimiento — Trabajo final de Sistemas Operativos II

Correspondencia entre la [estructura oficial](../ESTRUCTURA%20DEL%20TRABAJO%20FINAL%20-%20SISOPE-2.pdf)
y NEXUSTIX. Es una matriz documental: no ejecuta ni reemplaza pruebas.

**Estados:** Implementado · Parcialmente implementado · Preparado o configurado ·
Teórico/documentado · Pendiente · Sin evidencia experimental.

**Actualización (13/11/2026).** Se agregaron el servicio de nombres con latidos, la elección Bully, relojes vectoriales, Cristian y Berkeley, el failover automático de PostgreSQL con Patroni + etcd y el cambio automático de gateway. Ver `despliegue-nodos/ALTA_DISPONIBILIDAD.md`.

**Sobre la evidencia.** La línea base real es [RESULTADOS_DISTRIBUIDO.md](RESULTADOS_DISTRIBUIDO.md)
(Fedora, 03/11/2026). Las mejoras posteriores (entradas QR, escáner, paneles de observabilidad,
tolerancia a fallos, seguridad y laboratorio ampliado) todavía no se han ejecutado: figuran como
*Sin evidencia experimental*, aunque el código exista.

## I. Generalidades

| Apartado | Mecanismo relacionado | Archivo principal | Estado | Evidencia disponible | Evidencia faltante | Observaciones |
|---|---|---|---|---|---|---|
| 1.1 Descripción general | Venta distribuida de entradas | `README.md`, `docs/ARQUITECTURA.md` | Teórico/documentado | Documentación | Redacción final | |
| 1.2 Problema | Venta concurrente sin sobreventa ante fallos | `docs/ARQUITECTURA.md` | Pendiente | — | Redacción | Debe redactarlo el equipo |
| 1.3 Justificación | Concurrencia y disponibilidad | — | Pendiente | — | Redacción | |
| 1.4.1 Objetivo general | — | — | Pendiente | — | Redacción | |
| 1.4.2 Objetivos específicos | Unidades I–III | Panel académico | Pendiente | — | Redacción | |
| 1.5 Metodología | Desarrollo por paquetes | Historial de Git | Pendiente | Commits | Redacción | |
| 1.6 Arquitectura general | Microservicios, gateway, PostgreSQL | `docs/ARQUITECTURA.md`, `docs/ARQUITECTURA_DOS_PC.md` | Implementado | Documentos, mapa del Centro de Control | Captura actual del mapa | |
| 1.7 Herramientas | FastAPI, PostgreSQL, Docker, HAProxy | `requirements.txt`, Compose | Implementado | Archivos | Versiones finales | |
| 1.8 Limitaciones | Alcance real | `docs/LIMITACIONES.md` | Teórico/documentado | Documento | Actualizar tras los experimentos | Sin failover automático |

## II. Procesos y comunicación

| Apartado | Mecanismo relacionado | Archivo principal | Estado | Evidencia disponible | Evidencia faltante | Observaciones |
|---|---|---|---|---|---|---|
| 2.1 Descripción del problema | Procesos que cooperan en la venta | `docs/ARQUITECTURA.md` | Pendiente | — | Redacción | |
| 2.2 Estado del arte | — | — | Pendiente | — | Fuentes y redacción | Sin referencias inventadas |
| 2.3 Fundamentos | Conceptos del panel académico | `frontend/academico.js` | Teórico/documentado | Explicaciones breves | Sustento bibliográfico | |
| 2.4 Identificación de procesos | Instancias y métricas por proceso | `shared/diagnostics.py`, `frontend/observatorio.js` | Implementado · Sin evidencia experimental | Código | Captura de la tabla de procesos | PID y CPU no disponibles |
| 2.5 Arquitectura del sistema | Topología por inventario | `despliegue-nodos/inventory.py` | Implementado | Documentos | Captura del mapa | |
| 2.6 Identificación de servicios | Usuarios, Eventos, Compras | `usuarios/`, `eventos/`, `compras/` | Implementado | RESULTADOS_DISTRIBUIDO | — | |
| 2.7 Comunicación entre procesos | HTTP síncrono y outbox | `shared/checkout.py` | Implementado · Sin evidencia experimental | Código; base del 03/11 | Captura de comunicación con datos reales | Sin broker externo |
| 2.8 Protocolo seleccionado | HTTP/JSON | Servicios FastAPI | Implementado | Código | Justificación escrita | |
| 2.9 Diseño de la red | Redes Docker, LAN, puertos | `despliegue-nodos/render.py`, `despliegue-pc/` | Preparado o configurado | `docs/PUBLICACION_Y_LAN.md` | Evidencia LAN | |
| 2.10 Diseño de los servicios | Dominios y bases separadas | `docs/ARQUITECTURA.md` | Implementado | Documento | — | |
| 2.11 Implementación | FastAPI | `compras/main.py`, `shared/` | Implementado | Código, pruebas del 03/11 | Prueba de la versión final | |
| 2.12 Contenerización | Dockerfiles | `*/Dockerfile` | Implementado | Archivos, 12 contenedores del 03/11 | Reconstrucción final | |
| 2.13 Docker Compose | Local, distribuido, multinodo | `docker-compose*.yml`, `despliegue-nodos/` | Implementado | RESULTADOS_DISTRIBUIDO | Prueba final | |
| 2.14 Pruebas de comunicación | Suites y trazas | `tests/`, Observabilidad | Parcialmente implementado | 105 + 1 casos unitarios del 03/11 | Repetir con la versión final | |
| 2.15 Análisis de resultados | — | — | Pendiente | Base del 03/11 | Análisis final | |

## III. Nombres, sincronización, consistencia, replicación y tolerancia a fallas

| Apartado | Mecanismo relacionado | Archivo principal | Estado | Evidencia disponible | Evidencia faltante | Observaciones |
|---|---|---|---|---|---|---|
| 3.1 Servicio de nombres | Directorio replicado con registro, latido y resolución con TTL; DNS de Docker dentro de cada nodo | `shared/coordination.py`, panel Coordinación distribuida | Implementado | `tests/test_coordinacion.py` | Captura del directorio con dos nodos | Cada instancia guarda una copia del directorio |
| 3.2 Identificación de servicios | Inventario e identidad por instancia | `shared/node_operations.py` | Implementado · Sin evidencia experimental | Código | Muestreo de instancias | |
| 3.3 Sincronización | Lamport y reloj vectorial propagados en la Saga; Cristian y Berkeley contra el coordinador; exclusión mutua con bloqueos | `shared/coordination.py`, `shared/checkout.py`, `eventos/main.py` | Implementado | `tests/test_coordinacion.py` | Captura de relojes y comparación de eventos | El ajuste de Berkeley es lógico; no cambia la hora del SO |
| 3.4 Problemas de sincronización | Carrera por el mismo asiento | `eventos/main.py` | Implementado | Suites de concurrencia del 03/11 | Comparación del panel | |
| 3.5 Consistencia de datos | Invariante de inventario | `shared/catalog.py` | Implementado · Sin evidencia experimental | Auditoría previa | Captura por evento y zona | |
| 3.6 Modelo seleccionado | Fuerte local, Saga entre servicios | `shared/checkout.py` | Implementado | `docs/ARQUITECTURA.md` | Justificación escrita | No es ACID global |
| 3.7 Replicación | WAL primaria-standby | `despliegue-nodos/`, `postgres/` | Implementado | `tests/postgres/test_pg_replicacion.py` | Captura multinodo | Asíncrona en el despliegue conocido |
| 3.8 Diseño de las réplicas | Asignación por inventario | `despliegue-nodos/inventory.py` | Preparado o configurado | Inventarios | Evidencia de dos PC | |
| 3.9 Implementación de la replicación | Slots, entrypoint | `despliegue-nodos/postgres/` | Implementado | Pruebas PostgreSQL | Estado WAL final | |
| 3.10 Escenario de falla | Escenarios A–P | `shared/operations.py` | Implementado · Sin evidencia experimental | Código | Ejecutar y registrar | |
| 3.11 Detección de fallos | Latidos entre instancias (T, T + Δ), sondeos HAProxy y llave con TTL de Patroni | `shared/coordination.py`, `despliegue-nodos/render.py` | Implementado | `tests/test_coordinacion.py`, `tests/ha/laboratorio_patroni.py` | Captura de una instancia sospechosa/caída | |
| 3.12 Recuperación del servicio | Promoción automática de la réplica (Patroni + etcd), reincorporación con pg_rewind, recuperación de pedidos, elección Bully del coordinador | `despliegue-nodos/patroni/`, `shared/checkout.py` | Implementado | `docs/RESULTADOS_ALTA_DISPONIBILIDAD.md` | Ensayo con PC reales | Con `coordination.mode: patroni`; sin él, la promoción sigue siendo manual |
| 3.13 Tolerancia a fallas | Espejo de servicios en varias PC, failover de bases por mayoría, cambio automático de gateway en el navegador, modo degradado | `despliegue-nodos/`, `frontend/app.js` | Implementado | Laboratorio Docker: promoción en ~37 s, RPO 0 | Caída de una PC real | Requiere 3 votantes (dos PC + árbitro) |
| 3.14 Diseño de experimentos | Laboratorio | `frontend/laboratorio.js` | Implementado · Sin evidencia experimental | Código | Ejecución | |
| 3.15 Métricas | RTO, RPO, latencia, WAL | `shared/operations.py` | Implementado · Sin evidencia experimental | Código | Mediciones reales | Hoy: "Sin medición" |
| 3.16 Análisis de resultados | — | — | Pendiente | Base del 03/11 | Análisis final | |

## IV. Seguridad, implantación y caso de estudio

| Apartado | Mecanismo relacionado | Archivo principal | Estado | Evidencia disponible | Evidencia faltante | Observaciones |
|---|---|---|---|---|---|---|
| 4.1 Requerimientos | Roles, QR, secretos | `docs/ARQUITECTURA.md` | Pendiente | — | Redacción | |
| 4.2 Identificación y autenticación | Argon2id, JWT RS256, refresh HttpOnly | `usuarios/main.py`, `shared/auth_tokens.py` | Implementado | `tests/test_authentication.py` | Repetir con la versión final | |
| 4.3 Autorización | Roles en el backend | `compras/main.py`, `shared/catalog.py` | Implementado | Pruebas previas | Prueba de accesos denegados | |
| 4.4 Protección de comunicaciones | HTTPS opcional | `despliegue-externo/`, `despliegue-nodos/inventory.py` | Preparado o configurado | Plantillas | Certificados y prueba | HTTPS no activo; tráfico interno sin TLS |
| 4.5 Credenciales y variables | Secretos por archivo | `scripts/generar_secretos.py` | Implementado | Código | Captura del panel sin valores | |
| 4.6 Amenazas y vulnerabilidades | QR como credencial, split-brain, CORS | `docs/LIMITACIONES.md` | Teórico/documentado | Documento | Análisis formal | |
| 4.7 Modelo de implantación | Local, LAN, externo | `despliegue-nodos/`, `despliegue-externo/` | Preparado o configurado | Local y Fedora | Despliegue externo | Externo no ejecutado |
| 4.8 Arquitectura final | Topología del despliegue | Centro de Control | Parcialmente implementado | Documentos | Captura final | |
| 4.9 Integración de componentes | Compra de extremo a extremo | `shared/checkout.py` | Implementado · Sin evidencia experimental | Base del 03/11 | Recorrido con QR | |
| 4.10 Pruebas funcionales | Suites | `tests/` | Parcialmente implementado | 105 + 1 casos, 24 PostgreSQL del 03/11 | Repetir | Paquetes 4–12 sin prueba |
| 4.11 Pruebas de tolerancia a fallos | Laboratorio | `docs/PRUEBAS_RESILIENCIA.md` | Parcialmente implementado | Escenarios del 03/11 | Pérdida de PC y LAN | |
| 4.12 Pruebas de seguridad | Autenticación, permisos | `tests/test_authentication.py` | Parcialmente implementado | Autenticación | Permisos y QR | Sin pentesting |
| 4.13 Caso de estudio | Venta con concurrencia y fallos | `docs/PREPARACION_EXPOSICION.md` | Teórico/documentado | Guion | Ejecución | |
| 4.14 Comparación con un sistema real | — | — | Pendiente | — | Investigación | Sin referencias inventadas |
| 4.15 Evaluación final | — | — | Pendiente | — | Resultados | |
| 4.16 Limitaciones | Alcance | `docs/LIMITACIONES.md`, `despliegue-nodos/COORDINACION.md` | Teórico/documentado | Documentos | Actualización | |
| 4.17 Propuestas de mejora | TLS interno, 5 votantes, IP virtual de entrada | `despliegue-nodos/ALTA_DISPONIBILIDAD.md` | Teórico/documentado | — | — | Patroni + etcd ya está activo como modo opcional |

## V. Conclusiones, VI. Referencias y Anexos

| Apartado | Mecanismo relacionado | Archivo principal | Estado | Evidencia disponible | Evidencia faltante | Observaciones |
|---|---|---|---|---|---|---|
| 5.1 Conclusiones Unidad I | — | — | Pendiente | — | Resultados | |
| 5.2 Conclusiones Unidad II | — | — | Pendiente | — | Resultados | |
| 5.3 Conclusiones Unidad III | — | — | Pendiente | — | Resultados | |
| 5.4 Conclusión general | — | — | Pendiente | — | Resultados | |
| 5.5 Recomendaciones | Mejoras preparadas | `despliegue-nodos/COORDINACION.md` | Pendiente | — | Redacción | |
| VI. Referencias | Enlaces de COORDINACION.md | — | Pendiente | Enlaces oficiales | Bibliografía del equipo | |
| Anexo 1. Código fuente | Repositorio | `/` | Implementado | Código | Cambios sin commit | |
| Anexo 2. Dockerfiles | Imágenes | `*/Dockerfile` | Implementado | Archivos | — | |
| Anexo 3. Docker Compose | Despliegues | `docker-compose*.yml` | Implementado | Archivos | — | |
| Anexo 4. Arquitectura | Diagramas | `docs/ARQUITECTURA.md` | Implementado | Documentos | Figura final | |
| Anexo 5. Configuración de red | LAN, inventario | `docs/PUBLICACION_Y_LAN.md` | Preparado o configurado | Documentos | Datos reales sin secretos | |
| Anexo 6. Evidencias de pruebas | Resultados | `docs/evidencias/` | Parcialmente implementado | Base del 03/11, fuera de Git | Pruebas finales | |
| Anexo 7. Resultados experimentales | Reportes del Laboratorio | Evidencias de demostración | Sin evidencia experimental | Base del 03/11 | Experimentos finales | |
| Anexo 8. Tolerancia a fallos | Laboratorio | `docs/PRUEBAS_RESILIENCIA.md` | Sin evidencia experimental | Base del 03/11 | Pérdida de PC | |
| Anexo 9. Evidencias de seguridad | Auditoría y panel | Seguridad e implantación | Sin evidencia experimental | Código | Capturas | |
| Anexo 10. Repositorio | Git | `/` | Pendiente | — | URL final | |

## Entregables

| Entregable | Estado | Falta |
|---|---|---|
| 1. Prototipo de sistema distribuido | Implementado | Reconstruir y probar la versión final; informe técnico |
| 2. Sistema replicado y tolerante a fallas | Parcialmente implementado | Escenarios de falla con métricas reales |
| 3. Sistema seguro e integrado | Parcialmente implementado | Experimentos, análisis, video y presentación final |

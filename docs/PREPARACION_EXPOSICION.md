# Preparación de evidencias, video y exposición

Material de referencia. **Nada de este documento es un resultado.** Las evidencias están por
obtener; no uses capturas o cifras que no provengan de una ejecución real. El apartado de cada
evidencia corresponde a la [matriz](MATRIZ_CUMPLIMIENTO_SISOPE2.md).

## Lista de evidencias

### Unidad I

| Evidencia | Qué demostrar | Dónde obtenerla | Dato a observar | Apartado |
|---|---|---|---|---|
| Arquitectura | Componentes y conexiones | Centro de Control → Mapa | Nodos y enlaces HTTP, SQL y WAL | 1.6, 2.5, Anexo 4 |
| Servicios | Instancias activas | Centro de Control → Procesos | Servicio, instancia, nodo, estado | 2.4, 2.6 |
| Docker Compose | Servicios declarados | `docker-compose.yml`, inventario | Servicios y puertos, sin secretos | 2.12, 2.13, Anexo 3 |
| Comunicación | Modelos síncrono y asíncrono | Observabilidad → Comunicación | Llamada real y registros del outbox | 2.7, 2.8 |
| Despliegue | Instalación en la red | Microservicios y salud | Estado por nodo | 2.9, Anexo 5 |
| Funcionamiento | Compra de extremo a extremo | Portal y Seguir una compra | Pedido, pasos e instancia | 2.14, 2.15 |

### Unidad II

| Evidencia | Qué demostrar | Dónde obtenerla | Dato a observar | Apartado |
|---|---|---|---|---|
| Concurrencia | Dos reservas del mismo asiento | Observabilidad → Operaciones concurrentes | Aceptada y conflicto | 3.3, 3.4 |
| Consistencia | Invariante de inventario | Observabilidad → Inventario por evento | Inicial = disponible + reservadas + confirmadas | 3.5, 3.6 |
| Replicación | Primaria y standby | PostgreSQL y persistencia | Rol observado y canal | 3.7–3.9 |
| Estado WAL | Avance de la réplica | PostgreSQL y persistencia | LSN y diferencia en bytes | 3.9, 3.15 |
| Fallo de API | Continuidad con otra réplica | Laboratorio, escenario B | Antes, después e instancia | 3.10, 3.13 |
| Fallo de PC | Efecto real frente a proyección | Laboratorio, escenario I | Componentes sin respuesta | 3.10, Anexo 8 |
| Recuperación | Pedido o base recuperados | Observabilidad → Recuperación; escenario P | Estado anterior y posterior | 3.12 |
| Métricas | Valores medidos | Reporte del Laboratorio | Tipo de cada dato | 3.15, 3.16, Anexo 7 |

### Unidad III

| Evidencia | Qué demostrar | Dónde obtenerla | Dato a observar | Apartado |
|---|---|---|---|---|
| Autenticación | Inicio y expiración de sesión | Portal | Sesión aceptada y rechazada | 4.2 |
| Roles | Acceso denegado | Cuenta sin permisos | Mensaje y registro en Auditoría | 4.3, 4.12 |
| Validación de QR | Un solo uso | Control de acceso | Permitido y luego rechazado | 4.9, 4.12 |
| Auditoría | Acciones registradas | Seguridad e implantación | Acción, resultado, responsable | 4.12, Anexo 9 |
| Comunicaciones | Tipo de acceso | Seguridad e implantación | HTTP o HTTPS real | 4.4 |
| Implantación | Modalidad usada | Seguridad e implantación | Perfil y limitaciones | 4.7, 4.8 |
| Caso de estudio | Historia completa | Esta guía | Reporte vinculado | 4.13 |

Antes de usar una captura, comprueba que no muestre contraseñas, JWT, códigos QR completos o
datos personales.

## Caso de estudio central

**Venta distribuida de entradas con concurrencia y tolerancia a fallos.**

1. El organizador crea un evento.
2. Dos compradores intentan reservar un asiento.
3. El sistema impide la sobreventa.
4. Un comprador completa su pedido.
5. Se emite un QR.
6. El organizador valida el acceso.
7. Se observa el recorrido distribuido.
8. Se identifica la replicación de datos.
9. Se estudia una falla controlada.
10. Se registra el comportamiento degradado.
11. Se describe o ejecuta posteriormente la recuperación autorizada.
12. Se documentan resultados y limitaciones.

Si la caída física no se ejecuta, preséntala como proyección del Centro de Control.

## Guion del video

La duración no está definida; ajusta los tiempos de cada bloque cuando se conozca.

| Bloque | Contenido |
|---|---|
| Introducción | Proyecto, integrantes y curso |
| Problema y arquitectura | Por qué una venta necesita distribución; mapa del sistema |
| Demostración comercial | Selección, reserva, pago simulado, QR y validación |
| Demostración distribuida | Instancias, recorrido de una compra |
| Concurrencia y consistencia | Mismo asiento, un solo ganador, inventario |
| Replicación | Primaria, standby y WAL |
| Fallo y recuperación | Escenario ejecutado o proyección, etiquetado como tal |
| Seguridad | Roles, QR y secretos |
| Resultados | Solo mediciones obtenidas |
| Conclusiones | Logros y limitaciones |

## Diapositivas sugeridas

1. Presentación del proyecto.
2. Problema.
3. Objetivos.
4. Arquitectura.
5. Microservicios y comunicación.
6. Sincronización.
7. Consistencia.
8. Replicación.
9. Tolerancia a fallos.
10. Recuperación.
11. Seguridad.
12. Experimentos y métricas (solo con datos reales).
13. Limitaciones.
14. Conclusiones.

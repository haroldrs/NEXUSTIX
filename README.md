# NEXUSTIX

Venta de entradas con Usuarios, Eventos y Compras (FastAPI), PostgreSQL,
frontend estático y Nginx. Incluye autenticación, compras individuales y
grupales, idempotencia, saga con compensaciones, recuperación de operaciones
y entradas QR. Se conservan las rutas, los contratos y las migraciones existentes.

Este repositorio se prepara para revisión antes de publicarlo en GitHub privado.
**No se ha publicado ni desplegado en tres PC.** Las credenciales por defecto se retiraron y las coincidencias restantes tienen
revisión explícita: [hallazgos](docs/HALLAZGOS_PUBLICACION.md).
[Entrega y límites de publicación/LAN](docs/PUBLICACION_Y_LAN.md).

**Alta disponibilidad automática (13/11/2026):** con `coordination.mode: patroni`
y un árbitro, si se apaga cualquier PC la réplica de cada base se promueve sola
y el navegador cambia solo de gateway. Guía para el laboratorio (Tailscale,
árbitro, paquete sin Internet): [ALTA_DISPONIBILIDAD.md](despliegue-nodos/ALTA_DISPONIBILIDAD.md).
Resultados medidos: [RESULTADOS_ALTA_DISPONIBILIDAD.md](docs/RESULTADOS_ALTA_DISPONIBILIDAD.md).
El panel **Administración → Coordinación distribuida** muestra el directorio de
nombres con latidos, la elección Bully, los relojes de Lamport y vectorial, y la
sincronización de Cristian y Berkeley.

Nuevo despliegue configurable para **1, 2, 3 o más nodos**:
[inventario, preparación y comandos mínimos](despliegue-nodos/README.md).
Ruta independiente, sin ejecutar ni probar en esta entrega; prepara redundancia
y recuperación manual, no acredita alta disponibilidad física.

| Estado | Alcance |
|---|---|
| Implementado y probado localmente | Compras, concurrencia, QR e inventario; réplicas de Compras por Nginx; WAL, promoción, reintegración y PITR, con los límites de la evidencia |
| Línea base real | [Resultados del 03/11/2026](docs/RESULTADOS_DISTRIBUIDO.md); no equivale a una prueba entre equipos |
| Preparado, sin despliegue VM | [Manifiestos y procedimientos VM](despliegue-vm/README.md), [resultados de preparación](despliegue-vm/RESULTADOS.md) |
| Preparado y simulado, pendiente de despliegue físico | [Tres PC con PostgreSQL centralizado](despliegue-pc/README.md) |

Usuarios guarda identidades; Eventos controla el inventario; ambas instancias
de Compras comparten una única base lógica de compras y llaman a Eventos por
HTTP. Nginx distribuye solicitudes de Compras. Cada dominio tiene su PostgreSQL.
Detalle en [arquitectura distribuida](docs/ARQUITECTURA_DISTRIBUIDA.md).

Primer paso para un compañero: [instalación y clonación](docs/INSTALACION_EQUIPO.md).
Para revisar el código en un entorno Python ya preparado:

```bash
python -m pytest
python scripts/ejecutar_pruebas.py --capas unit
python scripts/revisar_publicacion.py
```

Los dos primeros ejecutan pruebas locales con SQLite temporal, dobles y
comprobaciones de configuración; no requieren Docker. El escáner solo lee
candidatos Git y muestra hallazgos redactados; un código distinto de cero exige
revisión. Las excepciones revisadas se vinculan a la huella exacta de cada línea;
cambiarla vuelve a abrir el hallazgo. No constituye una certificación de ausencia de secretos.
[Capas de pruebas y evidencias](docs/PRUEBAS_Y_EVIDENCIAS.md).

| Ruta | Responsabilidad |
|---|---|
| `usuarios/`, `eventos/`, `compras/`, `shared/` | APIs, modelos, migraciones y lógica compartida |
| `frontend/`, `compras_lb/` | Portal y balanceador |
| `postgres/`, `docker-compose*.yml` | Imágenes y topologías locales separadas |
| `despliegue-pc/` | Compose PC 1/2/3, HAProxy, secretos y operación protegida |
| `despliegue-nodos/` | Inventario YAML, Compose por nodo, gateways redundantes y PostgreSQL primario/standbys |
| `despliegue-vm/` | Infraestructura para invitados VM; no ejecutar sus scripts en PC físicas |
| `tests/test_*.py`, `tests/vm/` | Pruebas locales seleccionadas en `pytest.ini`; excepción: restauración Docker aislada |
| `tests/postgres/` | Integración y recuperación con PostgreSQL desechable, opt-in |
| `tests/resiliencia/`, `tests/staging/` | Pruebas con infraestructura real, opt-in; no usar sobre staging protegido |
| `locustfile.py`, `simular_trafico.py` | Carga; produce escrituras, no es diagnóstico de solo lectura |
| `scripts/`, `config/`, `docs/` | Administración, ejemplos y guías |
| `legacy/`, `docs/historico/` | Material histórico conservado para revisión, no guía de instalación |
| `resultados/`, `docs/evidencias/` | Evidencia local excluida de Git salvo el índice explicativo |

Entornos existentes que deben conservarse: `nexustix-auth-staging` y
`nexustix-distributed-lab`; también demo y recursos históricos. Un clon no debe
usar sus secretos ni sus volúmenes. Los nombres explícitos de los volúmenes en
Compose hacen insuficiente cambiar únicamente `-p` para aislar otra instalación
sobre el mismo Docker. Los comandos históricos de reset/fallos no forman parte
del inicio rápido. [Límites](docs/LIMITACIONES.md).

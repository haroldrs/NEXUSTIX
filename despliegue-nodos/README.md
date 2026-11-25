# Despliegue por inventario (1 a 32 computadoras)

Genera la configuración de cada computadora a partir de un único archivo de
inventario YAML: Docker Compose, secretos, gateway HAProxy, PostgreSQL con
Patroni, votantes etcd y la configuración del frontend. Las direcciones no están
en el código: todo sale del inventario.

## Archivos

| Archivo | Función |
|---|---|
| `inventory.py` | Lee y valida el inventario (nodos, roles, bases, votantes, puertos) |
| `render.py` | Genera el Compose y los archivos de cada nodo |
| `nodos.py` | Comando principal: `preparar` valida el inventario y genera la entrega |
| `unir.sh` | Empaqueta, envía, recibe, enciende, consulta y detiene un nodo |
| `patroni/` | Imagen PostgreSQL 15 + Patroni con failover automático |
| `postgres/` | Imagen PostgreSQL para el modo sin Patroni (recuperación manual) |
| `anunciar.py` | Identidad firmada opcional para incorporar nodos |
| `examples/` | Inventarios de ejemplo |

## Inventarios de ejemplo

| Ejemplo | Topología |
|---|---|
| `1-local.yml` | Una computadora |
| `2-distribuido.yml`, `3-espejo.yml`, `5-mixto.yml` | Varias computadoras con servicios replicados |
| `3-ha-preparada.yml`, `4-ha-heterogenea.yml` | Bases replicadas con recuperación manual ([RECUPERACION.md](RECUPERACION.md)) |
| `6-dos-pc-arbitro.yml` | Dos PC completas + un árbitro que solo vota; failover automático |
| `7-tres-pc-patroni.yml` | **Tres PC completas con failover automático (recomendado)** |

## Uso

```bash
python3 despliegue-nodos/nodos.py preparar inventario.yml ~/nexustix-entrega --imagenes-offline
```

`preparar` crea una carpeta por nodo y una carpeta `master/` con las claves, que
nunca debe salir de la computadora administradora. Cada nodo se enciende con:

```bash
bash despliegue-nodos/unir.sh iniciar ~/nexustix-entrega/pc1
```

Otros comandos de `unir.sh`: `empaquetar`, `enviar` (por Tailscale), `recibir`,
`estado` y `detener` (no borra datos).

La guía completa para varias computadoras, con Tailscale, firewall, orden de
arranque, prueba de failover y solución de problemas, está en
[ALTA_DISPONIBILIDAD.md](ALTA_DISPONIBILIDAD.md).

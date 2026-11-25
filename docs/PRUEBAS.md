# Pruebas

## Pruebas automáticas (sin Docker)

Usan SQLite temporal, archivos temporales y dobles de HTTP y Docker. No tocan
ninguna base real.

```bash
pip install -r requirements-test.txt
```

```bash
python -m pytest
```

`pytest.ini` define las suites que se ejecutan por defecto: autenticación, MFA y
bloqueo, inventario, recuperación de pedidos, coordinación distribuida, alta
disponibilidad, configuración del despliegue, operaciones, migraciones y respaldos.

El mismo conjunto, con un informe guardado en `resultados/`:

```bash
python scripts/ejecutar_pruebas.py --capas unit
```

## Pruebas con infraestructura (opcionales)

Crean bases o contenedores desechables, detienen servicios o escriben datos. Se
habilitan explícitamente y solo deben ejecutarse en un entorno de pruebas.

| Suite | Qué hace | Cómo habilitarla |
|---|---|---|
| `tests/postgres/` | Replicación, promoción y PITR con PostgreSQL desechable | `--allow-infrastructure-tests` y `NEXUSTIX_TEST_PG_ADMIN_URL` |
| `tests/staging/`, `tests/resiliencia/` | Pruebas HTTP de compras, caídas y recuperación sobre el entorno `pruebas` | `--allow-infrastructure-tests` y `NEXUSTIX_STAGING_HTTP=1` |
| `tests/test_backup_restore_isolated.py` | Respaldo y restauración con contenedores temporales | `--allow-infrastructure-tests` y `NEXUSTIX_BACKUP_DOCKER_TEST=1` |
| `tests/ha/laboratorio_patroni.py` | Failover de Patroni con 2 PC + árbitro simulados en Docker | `python3 tests/ha/laboratorio_patroni.py` |

## Pruebas de sistema

- **Concurrencia:** `scripts/prueba_concurrencia.py` lanza muchos compradores
  simultáneos contra uno o varios gateways y audita el inventario.
- **Caída de un nodo:** apagar un nodo con `docker compose kill` y comprobar
  que se vuelve a vender por otro (ver [RESULTADOS_ALTA_DISPONIBILIDAD.md](RESULTADOS_ALTA_DISPONIBILIDAD.md)).

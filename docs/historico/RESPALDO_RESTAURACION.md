# Respaldo y ensayo aislado de restauración NEXUSTIX

Este procedimiento genera copias lógicas de `usuarios_db`, `eventos_db` y
`compras_db`. No ejecuta migraciones sobre las bases principales. El comando
`verify` restaura copias en un contenedor PostgreSQL temporal conectado solo a
una red Docker `internal`; el directorio de datos de ese contenedor es `tmpfs`,
sin volumen nombrado y sin puertos publicados.

## Alcance y límites

- El Compose principal declara `postgres:15-alpine`. El respaldo además
  consulta y guarda `server_version` y `server_version_num` de cada servidor.
- Debe existir una ventana de mantenimiento: pausar registros, compras y
  cambios de inventario; reconciliar reservas pendientes. Un dump por base no
  constituye una instantánea transaccional entre tres bases independientes.
- El script exige la etiqueta exacta del proyecto Compose y los servicios
  esperados. Se niega a operar si detecta staging u otro proyecto.
- Se guardan hashes SHA-256, permisos restrictivos, estadísticas de negocio y
  conteos antes/después del dump. Un dump no se considera válido hasta que
  `verify` lo restaura y compara los resultados.
- En un fallo se conservan archivos parciales y el nombre de los recursos
  temporales para diagnóstico. Tras una verificación satisfactoria, el script
  elimina únicamente el contenedor y la red aislada que creó; nunca toca
  volúmenes ni contenedores principales.
- El ensayo opcional construye las imágenes locales de Usuarios y Compras y
  ejecuta Alembic contra las bases restauradas dentro de la red interna. No
  despliega esas imágenes ni usa URLs, credenciales o secretos de principal.

## Comandos propuestos (no ejecutar hasta autorizar lectura de producción)

Primero identifica el valor de la etiqueta Compose del entorno principal. En
este proyecto el nombre esperado por defecto es `project`; el script requiere
que se confirme explícitamente. No pases el nombre del proyecto staging.

```sh
umask 077
BACKUP_DIR="$HOME/nexustix-backups/$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/backup_main_databases.py backup \
  --output-dir "$BACKUP_DIR" \
  --compose-project project \
  --confirm-writes-paused
```

Este comando lee metadatos y conteos mediante `psql`, y produce un `pg_dump`
custom por contenedor principal. No lee archivos de secretos: usa el socket
local PostgreSQL dentro de cada contenedor. El indicador confirma que las
escrituras están pausadas; el script también rechaza reservas en estado
`reservada` y compara las estadísticas antes y después de los dumps. Si falla,
mantiene el directorio y los artefactos parciales.

La restauración aislada se ejecuta así:

```sh
python3 scripts/backup_main_databases.py verify \
  --backup-dir "$BACKUP_DIR" \
  --rehearse-migrations
```

`verify` no usa `docker-compose.yml`, los servicios principales ni secretos.
Verifica hashes, crea una red Docker interna sin salida, inicia PostgreSQL 15
con `tmpfs`, restaura los tres archivos y compara estadísticas detalladas de
usuarios; stock por evento; reservas por evento/estado; compras por estado y
evento; cantidades confirmadas; y journal por tipo. `--rehearse-migrations`
construye imágenes desde el código local y aplica las revisiones de Usuarios y
Compras solo a esas copias temporales. Si algo falla, deja los recursos
temporales aislados y escribe `verification.json` con el resultado.

## Archivos

- `scripts/backup_main_databases.py`: interfaz `backup`/`verify`, metadatos,
  conteos, hashes, restauración aislada y ensayo de migraciones.
- `tests/test_backup_procedure.py`: comprobaciones unitarias que no conectan a
  Docker ni a bases de datos.
- `tests/test_backup_restore_isolated.py`: ensayo Docker optativo con filas
  sintéticas. No busca ni usa contenedores principales/staging. Si falla,
  retiene sus archivos y recursos aislados; si pasa, retira solo sus fixtures.
- `docs/RESPALDO_RESTAURACION.md`: alcance, comandos y límites operativos.

Una salida `verified` indica que la restauración y las comparaciones
terminaron; no sustituye retención externa, cifrado del destino de backup ni
pruebas periódicas de recuperación ante desastre.

Para ejecutar las pruebas sin Docker:

```sh
python3 -m pytest -q tests/test_backup_procedure.py tests/test_backup_restore_isolated.py
```

Para el ensayo integral con datos sintéticos (crea redes internas, contenedores
`tmpfs` e imágenes locales de migración, sin acceder a los entornos existentes):

```sh
NEXUSTIX_BACKUP_DOCKER_TEST=1 python3 -m pytest -q tests/test_backup_restore_isolated.py
```

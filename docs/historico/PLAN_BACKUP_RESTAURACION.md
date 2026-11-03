# Plan de backup y recuperación de NEXUSTIX

Este documento complementa [`RESPALDO_RESTAURACION.md`](RESPALDO_RESTAURACION.md)
y `scripts/backup_main_databases.py`. No se ejecutó un backup ni una restauración
en esta misión. El procedimiento real previo quedó registrado como
`~/nexustix-backups/20261023T061536Z`, con `verification.json` en estado
`verified`, restauración de las tres bases y ensayo de migraciones, según la
auditoría anterior. Esa evidencia es histórica, no se revalidó aquí.

## Qué está disponible

- `backup`: dumps `pg_dump` custom independientes de `usuarios_db`,
  `eventos_db`, `compras_db`, metadata de versión/conteos, SHA-256 y permisos
  restrictivos. El script exige proyecto Compose explícito, pausa confirmada y
  rechaza reservas todavía `reservada`; conserva parciales al fallar.
- `verify`: restaura en PostgreSQL temporal por red Docker `internal`, con
  almacenamiento `tmpfs`, sin puertos/volúmenes de aplicación. Compara
  estadísticas y puede ensayar Alembic solo en copias restauradas.
- No hay WAL archiving, PITR, replicación continua ni standby PostgreSQL. Los
  dumps separados no forman snapshot atómico entre tres bases.

## Protocolo de captura

La operación necesita autorización/ventana separada porque lee todas las bases
principales y escribe artefactos locales. No se debe ejecutar durante compras,
registro ni cambios de stock.

```bash
cd "$HOME/Documents/Informatica/VIII CICLO/2.Sistemas operativos 2/Unit 2/Project"
umask 077
BACKUP_DIR="$HOME/nexustix-backups/$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/backup_main_databases.py backup \
  --output-dir "$BACKUP_DIR" \
  --compose-project project \
  --confirm-writes-paused
```

Alcance: consulta metadatos/conteos y escribe tres dumps/manifiestos; no aplica
migraciones ni modifica filas de negocio. Antes de ejecutarlo, verificar en
`docker compose -p project -f docker-compose.yml ps` que el proyecto coincide,
que el plan de ventana bloquea escrituras y que el destino no existe. El script
no sobreescribe destinos existentes.

Verifica únicamente una copia aprobada:

```bash
python3 scripts/backup_main_databases.py verify \
  --backup-dir "$BACKUP_DIR" \
  --rehearse-migrations
```

Esto crea recursos temporales aislados y puede construir imágenes locales para
el ensayo. No usar `down -v`, no pasar URLs principales y no restaurar encima de
un servicio existente. Un backup se llama **verificado** solo si `verification.json`
termina `status=verified`, las comparaciones coinciden y
`migrations_rehearsed=true` cuando se pidió ese ensayo.

## Retención y custodia propuesta (pendiente de implementar)

- Mantener al menos 7 diarios, 4 semanales y 12 mensuales; probar restauración
  mensual y después de cada migración.
- Copiar la segunda copia cifrada a almacenamiento externo/off-host con acceso
  mínimo. El disco/host fuente no puede ser la única ubicación.
- Separar permisos de operadores y custodios de claves; mantener material de
  cifrado fuera del backup. No guardar secretos de runtime dentro de los dumps.
- Verificar hashes tras transferencia y registrar fecha UTC, versión de
  PostgreSQL, versión de aplicación, revisiones Alembic, operador, ventana,
  resultados y ubicación (sin credenciales).
- No borrar copias antiguas automáticamente hasta tener rotación probada,
  retención aprobada y al menos una copia off-host verificada.

## Restauración ante pérdida/rollback

1. Declarar incidente y detener nuevas escrituras por un procedimiento
   operativo; conservar volúmenes/dumps originales para evidencia.
2. Restaurar primero en el entorno temporal aislado y comparar conteos, stock,
   reservas, claims, compras, operaciones y journal.
3. Revisar si los tres dumps comparten una ventana coherente. Si no hubo pausa
   coordinada, no asumir que los instantes coinciden.
4. Ensayar migraciones en copias; decidir si el despliegue de app es compatible.
5. En destino nuevo/vacío, restaurar las bases aprobadas, revisar migraciones,
   reconciliar por `operacion_id` y no abrir compras mientras haya divergencias.
6. La restauración a principal requiere plan y autorización específicos. No
   usar el verificador para sobrescribir una DB ni ejecutar downgrades
   destructivos.

Si se restaura `compras_db` más vieja que `eventos_db`, compras confirmadas
pueden no tener claim o reserva coherente. Si `eventos_db` es más vieja, stock
puede no reflejar compras. Claims pueden conservar una operación cuyo estado
externo no exista. Se debe bloquear el servicio, comparar, clasificar ambiguos y
reconciliar de forma manual con evidencia antes de reabrir. El journal no es
fuente de verdad transaccional.

#!/bin/sh
# Arranque conservador multinodo. No promueve, vacía, repara ni reutiliza copias parciales.
set -eu
umask 077
export LC_ALL=C
PGDATA="${PGDATA:-/var/lib/postgresql/data/pgdata}"
export PGDATA
state=/var/lib/nexustix-pg
archive="${NEXUSTIX_WAL_ARCHIVE:-/var/lib/postgresql/wal_archive}"
export NEXUSTIX_WAL_ARCHIVE="$archive"

fail() { printf '%s\n' "$1" >&2; exit 64; }
identifier() {
    case "$1" in ''|*[!a-z0-9_]*) fail 'Identificador PostgreSQL no válido.';; esac
    [ "${#1}" -le 63 ] || fail 'Identificador PostgreSQL demasiado largo.'
}
secret_copy() {
    [ -r "$1" ] || fail 'No se puede leer un secreto requerido.'
    [ -s "$1" ] || fail 'Un secreto requerido está vacío.'
    # Copia privada: los secretos Compose pueden pertenecer a root en Docker Engine.
    cp "$1" "$2"
    chown postgres:postgres "$2"
    chmod 0600 "$2"
}

[ "$(id -u)" = 0 ] || fail 'El wrapper necesita iniciar como root; PostgreSQL baja a postgres.'
[ "${1:-postgres}" = postgres ] || fail 'Use el entrypoint explícito para mantenimiento.'
case "${NEXUSTIX_PG_ROLE:?}" in primary|standby) ;; *) fail 'Rol PostgreSQL no válido.';; esac
identifier "${NEXUSTIX_PG_NODE_ID:?}"
case "${NEXUSTIX_PG_DOMAIN:?}" in usuarios|eventos|compras) ;; *) fail 'Base lógica no válida.';; esac
[ "${POSTGRES_DB:?}" = "${NEXUSTIX_PG_DOMAIN}_db" ] || fail 'Nombre de base incompatible con el dominio.'
[ "${POSTGRES_USER:-postgres}" = postgres ] || fail 'El administrador de PostgreSQL debe ser postgres.'
export POSTGRES_USER=postgres

[ ! -e "$PGDATA/NEXUSTIX_FENCED" ] && [ ! -e "$state/NEXUSTIX_FENCED" ] ||
    fail 'Nodo aislado: conserve los volúmenes y use una generación NUEVA al reintegrarlo.'
[ ! -e "$state/NEXUSTIX_CLONE_PENDING" ] ||
    fail 'Clonación incompleta conservada. Prepare otra generación de datos/estado/WAL.'

mkdir -p "$PGDATA" "$state" "$archive"
chown postgres:postgres "$PGDATA" "$state" "$archive"
chmod 0700 "$PGDATA" "$state" "$archive"
installation="${NEXUSTIX_INSTALLATION_ID:?}"
case "$installation" in ''|*[!0-9a-f]*) fail 'Identidad de instalación inválida.';; esac
[ "${#installation}" = 32 ] || fail 'Identidad de instalación inválida.'
identity="$installation/$NEXUSTIX_PG_DOMAIN/$NEXUSTIX_PG_NODE_ID"
if [ -e "$state/NEXUSTIX_IDENTITY" ]; then
    [ "$(cat "$state/NEXUSTIX_IDENTITY")" = "$identity" ] || fail 'Estos volúmenes pertenecen a otra instalación o nodo; no se reutilizan.'
else
    [ ! -s "$PGDATA/PG_VERSION" ] || fail 'Datos existentes sin identidad de instalación: no se adoptan automáticamente.'
    printf '%s\n' "$identity" > "$state/NEXUSTIX_IDENTITY"
    chown postgres:postgres "$state/NEXUSTIX_IDENTITY"
    chmod 0600 "$state/NEXUSTIX_IDENTITY"
fi
secret_copy "${POSTGRES_PASSWORD_FILE:?}" "$state/admin-password"
secret_copy "${NEXUSTIX_PG_APP_PASSWORD_FILE:?}" "$state/app-password"
secret_copy "${NEXUSTIX_PG_REPLICATION_PASSWORD_FILE:?}" "$state/replication-password"
export POSTGRES_PASSWORD_FILE="$state/admin-password"
export NEXUSTIX_PG_APP_PASSWORD_FILE="$state/app-password"
export NEXUSTIX_PG_REPLICATION_PASSWORD_FILE="$state/replication-password"

slots="${NEXUSTIX_PG_REPLICATION_SLOTS:-}"
slot_count=0
old_ifs=$IFS
IFS=,
for slot in $slots; do identifier "$slot"; slot_count=$((slot_count + 1)); done
IFS=$old_ifs
max_slots=$((slot_count + 8))
[ "$max_slots" -ge 16 ] || max_slots=16
sync_names=''
sync_count=0
IFS=,
for name in ${NEXUSTIX_PG_SYNCHRONOUS_STANDBYS:-}; do
    identifier "$name"
    sync_count=$((sync_count + 1))
    sync_names="${sync_names}${sync_names:+,}$name"
done
IFS=$old_ifs
if [ "$sync_count" -gt 0 ]; then
    quorum="${NEXUSTIX_PG_SYNCHRONOUS_QUORUM:-1}"
    case "$quorum" in ''|*[!0-9]*) fail 'Quórum síncrono no válido.';; esac
    [ "$quorum" -ge 1 ] && [ "$quorum" -le "$sync_count" ] || fail 'Quórum síncrono fuera de rango.'
    sync_names="ANY $quorum ($sync_names)"
fi

if [ -s "$PGDATA/PG_VERSION" ]; then
    [ ! -e "$PGDATA/recovery.signal" ] || fail 'Recuperación PITR detectada; requiere procedimiento separado.'
    if [ "$NEXUSTIX_PG_ROLE" = primary ] && [ -e "$PGDATA/standby.signal" ]; then
        fail 'Cambiar el inventario no promueve un standby. Aplique primero la promoción controlada.'
    fi
    if [ "$NEXUSTIX_PG_ROLE" = standby ] && [ ! -e "$PGDATA/standby.signal" ]; then
        fail 'Datos de un primario en un nodo declarado standby. Tras promover, actualice el inventario antes de reiniciar; una autorización antigua no permite otro arranque.'
    fi
else
    [ -z "$(ls -A "$PGDATA")" ] || fail 'PGDATA no está vacío y no es un clúster completo; se conserva.'
    if [ "$NEXUSTIX_PG_ROLE" = primary ] && [ "${NEXUSTIX_PG_ALLOW_INITDB:-0}" != 1 ]; then
        fail 'Una entrega regenerada requiere conservar los datos del primario; no inicializa otro clúster vacío.'
    fi
fi

if [ "$NEXUSTIX_PG_ROLE" = standby ]; then
    host="${NEXUSTIX_PG_PRIMARY_HOST:?}"
    port="${NEXUSTIX_PG_PRIMARY_PORT:-5432}"
    case "$host" in ''|*[!A-Za-z0-9._:-]*) fail 'Host primario no válido.';; esac
    case "$port" in ''|*[!0-9]*) fail 'Puerto primario no válido.';; esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail 'Puerto primario fuera de rango.'
    repl_password="$(cat "$state/replication-password")"
    [ -n "$repl_password" ] || fail 'Contraseña de replicación vacía.'
    newline='
'
    carriage_return="$(printf '\r')"
    case "$repl_password" in *"$newline"*|*"$carriage_return"*) fail 'Los secretos deben contener una sola línea.';; esac
    # pgpass, no password= en postgresql.auto.conf ni en argumentos visibles.
    escaped_password="$(printf '%s' "$repl_password" | sed 's/\\/\\\\/g; s/:/\\:/g')"
    escaped_host="$(printf '%s' "$host" | sed 's/:/\\:/g')"
    printf '%s:%s:replication:replicator:%s\n' "$escaped_host" "$port" "$escaped_password" > "$state/replication.pgpass"
    chown postgres:postgres "$state/replication.pgpass"
    chmod 0600 "$state/replication.pgpass"
    unset repl_password escaped_password
    export PGPASSFILE="$state/replication.pgpass"
    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        # pg_basebackup requiere destino vacío: marcador fuera de PGDATA y persistente.
        touch "$state/NEXUSTIX_CLONE_PENDING"
        if ! PGCONNECT_TIMEOUT=5 su-exec postgres pg_basebackup \
            -h "$host" -p "$port" -U replicator -D "$PGDATA" \
            -X stream --checkpoint=fast --no-clean --slot="$NEXUSTIX_PG_NODE_ID"; then
            fail 'Clonación fallida; datos parciales y marcador conservados. No se reintenta borrando.'
        fi
        touch "$PGDATA/standby.signal"
        chown postgres:postgres "$PGDATA/standby.signal"
        mv "$state/NEXUSTIX_CLONE_PENDING" "$state/NEXUSTIX_CLONE_COMPLETE"
    fi
    conninfo="host=$host port=$port user=replicator application_name=$NEXUSTIX_PG_NODE_ID passfile=$state/replication.pgpass connect_timeout=5"
    set -- "$@" -c "primary_conninfo=$conninfo" -c "primary_slot_name=$NEXUSTIX_PG_NODE_ID"
elif [ ! -s "$PGDATA/PG_VERSION" ]; then
    # También cubre CREATE DATABASE del entrypoint oficial, anterior a nuestros scripts.
    # Sólo psql de initdb hereda esto. Los clientes de aplicación usan synchronous_commit=on.
    export PGOPTIONS='-c synchronous_commit=local'
fi

# Un basebackup copia pg_hba del origen; el puente Docker del destino puede ser otro.
# No se escribe antes de initdb porque su directorio debe estar vacío.
if [ -s "$PGDATA/PG_VERSION" ]; then nexustix-pg-access; fi

set -- "$@" -c wal_level=replica -c "max_wal_senders=$max_slots" -c "max_replication_slots=$max_slots" \
    -c hot_standby=on -c wal_keep_size=512MB -c max_slot_wal_keep_size=2048MB \
    -c archive_mode=always -c 'archive_command=/usr/local/bin/archive-wal.sh %p %f' \
    -c synchronous_commit=on -c "synchronous_standby_names=$sync_names"
exec docker-entrypoint.sh "$@"

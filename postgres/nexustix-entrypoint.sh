#!/bin/sh
# Envoltorio del entrypoint oficial de PostgreSQL para NEXUSTIX.
#
#   nexustix-entrypoint.sh postgres ...       arranque normal (primario o standby)
#   nexustix-entrypoint.sh bootstrap-standby  solo clona el standby y termina
#
# Variables (todas opcionales):
#   NEXUSTIX_PG_UPSTREAM_HOST           primario del que clonar si el volumen está vacío
#   NEXUSTIX_PG_SLOT                    slot de replicación física en el primario
#   NEXUSTIX_REPLICATION_USER           rol de replicación (por defecto replicator)
#   NEXUSTIX_REPLICATION_PASSWORD_FILE  secreto con la contraseña del rol
set -eu

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
FENCE_FILE="$PGDATA/NEXUSTIX_FENCED"
REPL_USER="${NEXUSTIX_REPLICATION_USER:-replicator}"
UPSTREAM_PORT="${NEXUSTIX_PG_UPSTREAM_PORT:-5432}"

# El init oficial cambia a postgres; los secretos 0600 se leen antes, como root.
if [ "${NEXUSTIX_INIT_REPLICATION:-0}" = 1 ]; then
    NX_REPL_PASSWORD="$(cat "${NEXUSTIX_REPLICATION_PASSWORD_FILE:?secreto requerido}")"
    [ -n "$NX_REPL_PASSWORD" ] || exit 66
    export NX_REPL_PASSWORD
fi

log() { echo "[nexustix-postgres] $*" >&2; }

if [ -f "$FENCE_FILE" ]; then
    log "ARRANQUE RECHAZADO: este nodo fue aislado (fencing) durante una promoción."
    log "Otro nodo es ahora el primario; arrancar este podría crear dos primarios."
    log "Reincorpóralo como standby con: python3 scripts/db_ha.py <entorno> rejoin <base> <servicio>"
    sed 's/^/[nexustix-postgres]   /' "$FENCE_FILE" >&2 || true
    exit 64
fi

clone_standby() {
    upstream="$1"
    if [ -s "$PGDATA/PG_VERSION" ]; then
        log "El volumen ya contiene un clúster; no se clona de nuevo."
        return 0
    fi
    if [ -n "$(ls -A "$PGDATA" 2>/dev/null || true)" ]; then
        log "ERROR: $PGDATA no está vacío pero no contiene un clúster válido; no se sobrescribe."
        exit 65
    fi
    if [ -z "${NEXUSTIX_REPLICATION_PASSWORD_FILE:-}" ] || [ ! -r "$NEXUSTIX_REPLICATION_PASSWORD_FILE" ]; then
        log "ERROR: falta NEXUSTIX_REPLICATION_PASSWORD_FILE para clonar el standby."
        exit 66
    fi
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 0700 "$PGDATA"
    log "Esperando al primario $upstream..."
    tries=0
    until pg_isready -h "$upstream" -p "$UPSTREAM_PORT" -q; do
        tries=$((tries + 1))
        if [ "$tries" -ge 90 ]; then
            log "ERROR: el primario $upstream no respondió."
            exit 67
        fi
        sleep 2
    done
    slot_args=""
    if [ -n "${NEXUSTIX_PG_SLOT:-}" ]; then
        slot_args="--slot=$NEXUSTIX_PG_SLOT"
    fi
    log "Clonando standby desde $upstream (pg_basebackup, WAL en streaming)..."
    # PGPASSWORD solo vive en el entorno de este proceso; -R escribe
    # primary_conninfo y standby.signal para arrancar en modo standby.
    # shellcheck disable=SC2086
    if ! PGPASSWORD="$(cat "$NEXUSTIX_REPLICATION_PASSWORD_FILE")" su-exec postgres \
        pg_basebackup -h "$upstream" -p "$UPSTREAM_PORT" -U "$REPL_USER" -D "$PGDATA" \
        -R -X stream --checkpoint=fast --progress $slot_args; then
        # El directorio estaba vacío antes de este intento: lo que haya ahora es
        # una copia parcial propia; se retira para que el reinicio reintente limpio.
        find "$PGDATA" -mindepth 1 -delete
        log "ERROR: pg_basebackup falló (¿se ejecutó 'db_ha.py <entorno> enable'?)."
        exit 69
    fi
    log "Standby clonado desde $upstream."
}

if [ "${1:-}" = "bootstrap-standby" ]; then
    if [ -z "${NEXUSTIX_PG_UPSTREAM_HOST:-}" ]; then
        log "ERROR: bootstrap-standby requiere NEXUSTIX_PG_UPSTREAM_HOST."
        exit 68
    fi
    clone_standby "$NEXUSTIX_PG_UPSTREAM_HOST"
    exit 0
fi

if [ -n "${NEXUSTIX_PG_UPSTREAM_HOST:-}" ] && [ ! -s "$PGDATA/PG_VERSION" ]; then
    clone_standby "$NEXUSTIX_PG_UPSTREAM_HOST"
fi

exec docker-entrypoint.sh "$@"

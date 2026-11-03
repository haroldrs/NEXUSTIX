#!/bin/sh
# Clonación conservadora exclusiva de VMs: nunca borra una copia parcial.
set -eu
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
if [ -e "$PGDATA/NEXUSTIX_FENCED" ]; then
    echo 'Nodo aislado: conservar este volumen y reintegrar en uno NUEVO.' >&2
    exit 64
fi
marker="$(dirname "$PGDATA")/NEXUSTIX_CLONE_PENDING"
if [ -e "$marker" ]; then
    echo 'Clonación incompleta: conservar evidencia y usar otra generación de volumen.' >&2
    exit 65
fi
if [ "${NEXUSTIX_INIT_REPLICATION:-0}" = 1 ]; then
    NX_REPL_PASSWORD="$(cat "${NEXUSTIX_REPLICATION_PASSWORD_FILE:?}")"
    [ -n "$NX_REPL_PASSWORD" ] || exit 66
    export NX_REPL_PASSWORD
fi
if [ -n "${NEXUSTIX_PG_UPSTREAM_HOST:-}" ] && [ ! -s "$PGDATA/PG_VERSION" ]; then
    # Una copia parcial o un volumen desconocido jamás se vacía automáticamente.
    [ -z "$(ls -A "$PGDATA" 2>/dev/null)" ] || { echo 'Destino no vacío; se conserva.' >&2; exit 65; }
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 0700 "$PGDATA"
    # Marcador fuera del directorio que pg_basebackup exige vacío.
    touch "$marker"
    repl_password="$(cat "${NEXUSTIX_REPLICATION_PASSWORD_FILE:?}")"
    [ -n "$repl_password" ] || exit 66
    # --no-clean conserva los archivos incluso si PostgreSQL detecta un fallo.
    if ! PGPASSWORD="$repl_password" PGCONNECT_TIMEOUT=5 su-exec postgres pg_basebackup \
        -h "$NEXUSTIX_PG_UPSTREAM_HOST" -p "${NEXUSTIX_PG_UPSTREAM_PORT:-5432}" \
        -U replicator -D "$PGDATA" -R -X stream --checkpoint=fast --no-clean \
        --slot="${NEXUSTIX_PG_SLOT:?}"; then
        echo 'Clonación fallida; copia parcial conservada. No se reinicializa.' >&2
        exit 69
    fi
    mv "$marker" "$PGDATA/NEXUSTIX_CLONE_COMPLETE"
    unset repl_password
fi
exec docker-entrypoint.sh "$@"

#!/bin/sh
# Operaciones EXPLÍCITAS dentro de un único contenedor. Nunca se invoca al arrancar.
# No controla Docker, otros nodos, firewall, SSH ni el sistema anfitrión.
set -eu
umask 077
state=/var/lib/nexustix-pg
db="${POSTGRES_DB:?}"
query() { psql -X -v ON_ERROR_STOP=1 -At -U postgres -d "$db" -c "$1"; }
fail() { printf '%s\n' "$1" >&2; exit 64; }
operation="${1:-status}"
if [ "$#" -gt 0 ]; then shift; fi
case "$operation" in
status)
    [ "$#" = 0 ] || fail 'status no acepta argumentos.'
    query "SELECT json_build_object('system_identifier', (SELECT system_identifier::text FROM pg_control_system()), 'recovery', pg_is_in_recovery(), 'timeline', (SELECT timeline_id FROM pg_control_checkpoint()), 'received_lsn', pg_last_wal_receive_lsn(), 'replayed_lsn', pg_last_wal_replay_lsn(), 'replay_paused', CASE WHEN pg_is_in_recovery() THEN pg_is_wal_replay_paused() ELSE false END, 'synchronous_standby_names', current_setting('synchronous_standby_names'));"
    query "SELECT json_build_object('application_name', application_name, 'state', state, 'sync_state', sync_state, 'sent_lsn', sent_lsn, 'flush_lsn', flush_lsn, 'replay_lsn', replay_lsn) FROM pg_stat_replication;"
    query "SELECT json_build_object('slot_name', slot_name, 'active', active, 'restart_lsn', restart_lsn, 'wal_status', wal_status, 'safe_wal_size', safe_wal_size) FROM pg_replication_slots;"
    ;;
prepare-slots)
    [ "$#" = 0 ] || fail 'prepare-slots no acepta argumentos.'
    [ "$(query 'SELECT pg_is_in_recovery();')" = f ] || fail 'Los slots se preparan en el primario confirmado.'
    [ ! -e "$state/NEXUSTIX_FENCED" ] && [ ! -e "$PGDATA/NEXUSTIX_FENCED" ] || fail 'Nodo aislado.'
    # Sin commit remoto: evita bloquear la preparación de la réplica que aún debe arrancar.
    export PGOPTIONS='-c synchronous_commit=local'
    old_ifs=$IFS
    IFS=,
    for slot in ${NEXUSTIX_PG_REPLICATION_SLOTS:-}; do
        case "$slot" in ''|*[!a-z0-9_]*) fail 'Slot no válido.';; esac
        export NX_SLOT="$slot"
        psql -X -v ON_ERROR_STOP=1 -U postgres -d "$db" <<'SQL'
\getenv slot NX_SLOT
SELECT pg_create_physical_replication_slot(:'slot', true)
WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = :'slot');
SQL
    done
    IFS=$old_ifs
    unset NX_SLOT
    ;;
promote)
    # La evidencia física de fencing la aporta el operador. Esta herramienta no la presume.
    [ "$#" = 4 ] || fail 'Uso: promote ID_SISTEMA LSN_MINIMO ID_PRIMARIO_AISLADO --fencing-confirmado-rpo-aceptado'
    expected_system=$1
    target_lsn=$2
    fenced_id=$3
    [ "$4" = --fencing-confirmado-rpo-aceptado ] || fail 'Falta aceptación explícita del fencing y del RPO.'
    case "$expected_system" in ''|*[!0-9]*) fail 'Identificador de sistema no válido.';; esac
    case "$target_lsn" in ''|*[!0-9A-Fa-f/]*) fail 'LSN no válido.';; esac
    case "$target_lsn" in */*) ;; *) fail 'LSN no válido.';; esac
    case "$fenced_id" in ''|*[!a-z0-9_]*) fail 'Identificador de primario aislado no válido.';; esac
    [ "$fenced_id" != "${NEXUSTIX_PG_NODE_ID:?}" ] || fail 'El candidato no puede ser el primario aislado.'
    [ "$fenced_id" = "${NEXUSTIX_PG_PRIMARY_ID:?}" ] || fail 'El primario aislado no coincide con el inventario del candidato.'
    [ "${NEXUSTIX_PG_ROLE:?}" = standby ] || fail 'La promoción controlada parte de un standby del inventario.'
    [ ! -e "$state/NEXUSTIX_FENCED" ] && [ ! -e "$PGDATA/NEXUSTIX_FENCED" ] || fail 'Nodo aislado.'
    [ "$(query 'SELECT pg_is_in_recovery();')" = t ] || fail 'El candidato no está en recuperación.'
    [ "$(query 'SELECT system_identifier::text FROM pg_control_system();')" = "$expected_system" ] || fail 'El candidato pertenece a otro clúster.'
    [ "$(query 'SELECT pg_is_wal_replay_paused();')" = f ] || fail 'La reproducción de WAL está pausada.'
    export NX_MINIMUM_LSN="$target_lsn"
    ready="$(psql -X -v ON_ERROR_STOP=1 -At -U postgres -d "$db" <<'SQL'
\getenv minimum_lsn NX_MINIMUM_LSN
SELECT COALESCE(pg_last_wal_replay_lsn() >= :'minimum_lsn'::pg_lsn
    AND pg_last_wal_replay_lsn() >= pg_last_wal_receive_lsn(), false);
SQL
)"
    [ "$ready" = t ] || fail 'El candidato no ha aplicado el WAL mínimo y todo el WAL recibido.'
    unset NX_MINIMUM_LSN
    # Permiso persistente antes de promover: un timeout/reinicio no revierte una promoción.
    # No certifica que el fencing físico exista ni convierte esta operación en automática.
    {
        printf '%s\n' "$NEXUSTIX_PG_NODE_ID"
        printf 'system_identifier=%s\nfenced_node=%s\nminimum_lsn=%s\n' "$expected_system" "$fenced_id" "$target_lsn"
        date -u '+authorized_utc=%Y-%m-%dT%H:%M:%SZ'
    } > "$state/NEXUSTIX_PROMOTED"
    chown postgres:postgres "$state/NEXUSTIX_PROMOTED"
    chmod 0600 "$state/NEXUSTIX_PROMOTED"
    sync
    [ "$(query 'SELECT pg_promote(true, 60);')" = t ] || fail 'Promoción pendiente o fallida; inspeccione el estado, no promueva otro nodo.'
    [ "$(query 'SELECT pg_is_in_recovery();')" = f ] || fail 'El candidato sigue en recuperación.'
    printf '%s\n' 'Promoción realizada. Actualice el inventario y prepare los slots antes de reanudar escrituras.'
    ;;
*) fail 'Operación no válida. Use status, prepare-slots o promote.';;
esac

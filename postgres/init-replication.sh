#!/bin/sh
# Solo para un primario NUEVO configurado explícitamente para el laboratorio.
set -eu
[ "${NEXUSTIX_INIT_REPLICATION:-0}" = 1 ] || exit 0
if [ -z "${NX_REPL_PASSWORD:-}" ]; then
    NX_REPL_PASSWORD="$(cat "$NEXUSTIX_REPLICATION_PASSWORD_FILE")"
fi
[ -n "$NX_REPL_PASSWORD" ] || exit 66
export NX_REPL_PASSWORD
export NX_REPL_SLOT="${NEXUSTIX_REPLICA_SLOT:?slot requerido}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv repl_password NX_REPL_PASSWORD
\getenv repl_slot NX_REPL_SLOT
SELECT 'CREATE ROLE replicator WITH REPLICATION LOGIN' WHERE NOT EXISTS
  (SELECT 1 FROM pg_roles WHERE rolname='replicator') \gexec
SELECT format('ALTER ROLE replicator WITH REPLICATION LOGIN PASSWORD %L', :'repl_password') \gexec
SELECT pg_create_physical_replication_slot(:'repl_slot') WHERE NOT EXISTS
  (SELECT 1 FROM pg_replication_slots WHERE slot_name=:'repl_slot');
SQL
unset NX_REPL_PASSWORD
echo 'host replication replicator all scram-sha-256' >> "$PGDATA/pg_hba.conf"

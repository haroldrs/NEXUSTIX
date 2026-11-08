#!/bin/sh
# Sólo ejecutado por la imagen oficial al inicializar un primario NUEVO.
set -eu
umask 077
[ "${NEXUSTIX_PG_ROLE:?}" = primary ] || exit 64
NX_APP_PASSWORD="$(cat "${NEXUSTIX_PG_APP_PASSWORD_FILE:?}")"
NX_REPL_PASSWORD="$(cat "${NEXUSTIX_PG_REPLICATION_PASSWORD_FILE:?}")"
[ -n "$NX_APP_PASSWORD" ] && [ -n "$NX_REPL_PASSWORD" ] || exit 66
export NX_APP_PASSWORD NX_REPL_PASSWORD
psql -X -v ON_ERROR_STOP=1 --username postgres --dbname "$POSTGRES_DB" <<'SQL'
SET synchronous_commit = local;
\getenv app_password NX_APP_PASSWORD
\getenv repl_password NX_REPL_PASSWORD
\getenv db POSTGRES_DB
CREATE ROLE nexustix_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE replicator LOGIN REPLICATION PASSWORD :'repl_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
-- Lectura de canales WAL para el diagnóstico administrativo; no concede promoción.
GRANT pg_monitor TO nexustix_app;
GRANT EXECUTE ON FUNCTION pg_control_system() TO nexustix_app;
ALTER DATABASE :"db" OWNER TO nexustix_app;
ALTER SCHEMA public OWNER TO nexustix_app;
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO nexustix_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
unset NX_APP_PASSWORD NX_REPL_PASSWORD

# CIDRs explícitos del inventario (LAN y puente de este nodo).
nexustix-pg-access

old_ifs=$IFS
IFS=,
for slot in ${NEXUSTIX_PG_REPLICATION_SLOTS:-}; do
    export NX_SLOT="$slot"
    psql -X -v ON_ERROR_STOP=1 --username postgres --dbname "$POSTGRES_DB" <<'SQL'
SET synchronous_commit = local;
\getenv slot NX_SLOT
SELECT pg_create_physical_replication_slot(:'slot', true)
WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = :'slot');
SQL
done
IFS=$old_ifs
unset NX_SLOT

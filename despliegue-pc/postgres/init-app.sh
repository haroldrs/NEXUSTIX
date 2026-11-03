#!/bin/sh
set -eu
# Se ejecuta exclusivamente al inicializar un PGDATA nuevo.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$NEXUSTIX_APP_PASSWORD" --set=db="$POSTGRES_DB" <<'SQL'
CREATE ROLE nexustix_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER DATABASE :"db" OWNER TO nexustix_app;
ALTER SCHEMA public OWNER TO nexustix_app;
SQL
# El superusuario solo es accesible por socket local dentro del contenedor.
cat > "$PGDATA/pg_hba.conf" <<'HBA'
local all all trust
host all nexustix_app 0.0.0.0/0 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::0/0 reject
HBA

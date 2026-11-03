#!/bin/sh
# Healthcheck de un nodo PostgreSQL de NEXUSTIX (primario o standby).
# - pg_isready por TCP: el servidor temporal que usa initdb en el primer
#   arranque solo escucha en el socket local, así que no cuenta como listo.
# - SELECT 1 por el socket local: el servidor responde consultas reales.
set -eu
db="${POSTGRES_DB:-postgres}"
pg_isready -h 127.0.0.1 -p 5432 -U postgres -d "$db" -q
[ "$(psql -X -A -t -U postgres -d "$db" -c 'SELECT 1' 2>/dev/null)" = "1" ]

#!/bin/sh
# Patroni lo ejecuta una sola vez, en el nodo que inicializa el clúster.
# $1 es la cadena de conexión de superusuario que entrega Patroni.
set -eu
psql "$1" -v ON_ERROR_STOP=1 -v app_password="$NEXUSTIX_APP_PASSWORD" -v dbname="$NEXUSTIX_PG_DATABASE" <<'SQL'
SELECT format('CREATE ROLE nexustix_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexustix_app') \gexec
SELECT format('CREATE DATABASE %I OWNER nexustix_app', :'dbname')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec
SQL

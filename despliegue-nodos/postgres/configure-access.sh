#!/bin/sh
# Reglas exclusivas de este despliegue. Se regeneran también al seguir a otro primario.
set -eu
umask 077
: "${PGDATA:?}" "${POSTGRES_DB:?}" "${NEXUSTIX_PG_ALLOWED_CIDRS:?}"
access_file="$PGDATA/pg_hba.nexustix.pending"
{
    printf '%s\n' 'local all postgres trust' 'local all all reject'
    old_ifs=$IFS
    IFS=,
    for cidr in $NEXUSTIX_PG_ALLOWED_CIDRS; do
        case "$cidr" in ''|*[!0-9a-fA-F.:/]*|0.0.0.0/0|::/0) echo 'CIDR no admitido.' >&2; exit 64;; esac
        case "$cidr" in */*) ;; *) echo 'Se requiere una máscara CIDR explícita.' >&2; exit 64;; esac
        printf 'host %s nexustix_app %s scram-sha-256\n' "$POSTGRES_DB" "$cidr"
        printf 'host replication replicator %s scram-sha-256\n' "$cidr"
    done
    IFS=$old_ifs
    printf '%s\n' 'host all all 0.0.0.0/0 reject' 'host all all ::0/0 reject'
} > "$access_file"
chmod 0600 "$access_file"
if [ "$(id -u)" = 0 ]; then chown postgres:postgres "$access_file"; fi
mv "$access_file" "$PGDATA/pg_hba.conf"

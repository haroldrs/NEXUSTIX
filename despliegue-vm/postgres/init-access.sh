#!/bin/sh
# Solo initdb de volúmenes NUEVOS. Las direcciones se validan al preparar el paquete.
set -eu
: "${VM_APP_HOST:?}" "${VM_PRIMARY_HOST:?}" "${VM_STANDBY_HOST:?}"
cat > "$PGDATA/pg_hba.conf" <<HBA
local all postgres trust
host all postgres 127.0.0.1/32 scram-sha-256
host all postgres ${VM_APP_HOST}/32 scram-sha-256
host all postgres ${VM_PRIMARY_HOST}/32 scram-sha-256
host all postgres ${VM_STANDBY_HOST}/32 scram-sha-256
host replication replicator ${VM_PRIMARY_HOST}/32 scram-sha-256
host replication replicator ${VM_STANDBY_HOST}/32 scram-sha-256
HBA

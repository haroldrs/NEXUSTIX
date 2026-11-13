#!/bin/sh
# Lee los secretos como root, los entrega a Patroni por entorno y lo ejecuta
# como el usuario postgres. Patroni decide si este nodo inicia el clúster,
# sigue a la principal o se reincorpora tras una conmutación.
set -eu
read_secret() { tr -d '\r\n' < "$1"; }
export PATRONI_SUPERUSER_USERNAME=postgres
export PATRONI_SUPERUSER_PASSWORD="$(read_secret "$NEXUSTIX_PG_ADMIN_PASSWORD_FILE")"
export PATRONI_REPLICATION_USERNAME=replicator
export PATRONI_REPLICATION_PASSWORD="$(read_secret "$NEXUSTIX_PG_REPLICATION_PASSWORD_FILE")"
NEXUSTIX_APP_PASSWORD="$(read_secret "$NEXUSTIX_PG_APP_PASSWORD_FILE")"
export NEXUSTIX_APP_PASSWORD
mkdir -p /var/lib/postgresql/data /run/postgresql
chown -R postgres:postgres /var/lib/postgresql/data /run/postgresql
chmod 0700 /var/lib/postgresql/data

# La primera vez, los nodos no preferidos esperan a que la principal declarada
# en el inventario inicie el clúster, para que la topología inicial sea la prevista.
if [ "${NEXUSTIX_PATRONI_PREFERRED:-1}" != "1" ] && [ ! -s /var/lib/postgresql/data/pgdata/PG_VERSION ]; then
    /usr/local/bin/python3 - <<'PY' || true
import os, time, urllib.request
url = os.environ.get("NEXUSTIX_PATRONI_PREFERRED_URL", "")
deadline = time.monotonic() + int(os.environ.get("NEXUSTIX_PATRONI_BOOT_WAIT", "180"))
while url and time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url + "/primary", timeout=3) as answer:
            if answer.status == 200:
                break
    except Exception:
        pass
    time.sleep(3)
PY
fi
exec gosu postgres patroni /etc/nexustix/patroni.yml

#!/bin/sh
set -eu
# Solo lee secretos del despliegue nuevo. La imagen oficial baja privilegios después.
POSTGRES_PASSWORD=$(cat "${POSTGRES_PASSWORD_FILE:?}")
NEXUSTIX_APP_PASSWORD=$(cat "${NEXUSTIX_APP_PASSWORD_FILE:?}")
[ -n "$POSTGRES_PASSWORD" ] && [ -n "$NEXUSTIX_APP_PASSWORD" ]
export POSTGRES_PASSWORD NEXUSTIX_APP_PASSWORD
unset POSTGRES_PASSWORD_FILE
exec docker-entrypoint.sh "$@"

#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    rm -rf /tmp/app-secrets
    install -d -m 0700 /tmp/app-secrets
    if [ "${DFO_SPM_PREPARE_ATTACHMENTS:-false}" = "true" ]; then
        chown 10001:10001 /app/data/attachments
    fi

    if [ -n "${DFO_SPM_DB_PASSWORD_FILE:-}" ]; then
        install -m 0400 "${DFO_SPM_DB_PASSWORD_FILE}" /tmp/app-secrets/db_password
        export DFO_SPM_DB_PASSWORD_FILE=/tmp/app-secrets/db_password
    fi
    if [ -n "${DFO_SPM_DEFAULT_ADMIN_PASSWORD_FILE:-}" ]; then
        install -m 0400 "${DFO_SPM_DEFAULT_ADMIN_PASSWORD_FILE}" /tmp/app-secrets/admin_password
        export DFO_SPM_DEFAULT_ADMIN_PASSWORD_FILE=/tmp/app-secrets/admin_password
    fi
    chown -R 10001:10001 /tmp/app-secrets

    exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi

exec "$@"

#!/bin/sh
set -eu

# Canonical keys take precedence; retain support for installed legacy stacks.
for key in PREPARE_ATTACHMENTS DB_PASSWORD_FILE DEFAULT_ADMIN_PASSWORD_FILE; do
    eval 'present=${SPM_'"$key"'+x}'
    if [ "$present" != x ]; then
        eval 'value=${DFO_SPM_'"$key"':-}'
        export "SPM_${key}=$value"
    fi
done

if [ "$(id -u)" = "0" ]; then
    rm -rf /tmp/app-secrets
    install -d -m 0700 /tmp/app-secrets
    if [ "${SPM_PREPARE_ATTACHMENTS:-false}" = "true" ]; then
        chown 10001:10001 /app/data/attachments
    fi

    if [ -n "${SPM_DB_PASSWORD_FILE:-}" ]; then
        install -m 0400 "${SPM_DB_PASSWORD_FILE}" /tmp/app-secrets/db_password
        export SPM_DB_PASSWORD_FILE=/tmp/app-secrets/db_password
    fi
    if [ -n "${SPM_DEFAULT_ADMIN_PASSWORD_FILE:-}" ]; then
        install -m 0400 "${SPM_DEFAULT_ADMIN_PASSWORD_FILE}" /tmp/app-secrets/admin_password
        export SPM_DEFAULT_ADMIN_PASSWORD_FILE=/tmp/app-secrets/admin_password
    fi
    chown -R 10001:10001 /tmp/app-secrets

    exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi

exec "$@"

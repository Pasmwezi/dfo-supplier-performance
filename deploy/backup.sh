#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
BACKUP_ROOT="${BACKUP_ROOT:-${ROOT_DIR}/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
LOCK_FILE="${LOCK_FILE:-${ROOT_DIR}/backups/.operations.lock}"
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${ROOT_DIR}/compose.production.yml")

if [[ ! -f "${ENV_FILE}" ]]; then
  printf 'Missing production environment file: %s\n' "${ENV_FILE}" >&2
  exit 1
fi
if [[ ! "${RETENTION_DAYS}" =~ ^[0-9]+$ ]]; then
  printf 'RETENTION_DAYS must be a non-negative integer.\n' >&2
  exit 1
fi

mkdir -p "${BACKUP_ROOT}" "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  printf 'Another backup or restore operation is already running.\n' >&2
  exit 1
fi
stamp="$(date -u +%Y%m%dT%H%M%S%NZ)"
temporary="$(mktemp -d "${BACKUP_ROOT}/.dfo-spm-${stamp}.XXXXXX")"
destination="${BACKUP_ROOT}/dfo-spm-${stamp}"
services_stopped=false
cleanup() {
  status=$?
  rm -rf "${temporary}"
  if [[ "${services_stopped}" == true ]]; then
    "${COMPOSE[@]}" up -d --wait app caddy >/dev/null || true
  fi
  exit "${status}"
}
trap cleanup EXIT

"${COMPOSE[@]}" stop caddy app
services_stopped=true

"${COMPOSE[@]}" exec -T postgres sh -ceu '
  export PGPASSWORD="$(cat /run/secrets/db_password)"
  pg_dump --format=custom --no-owner --no-acl --username "$POSTGRES_USER" "$POSTGRES_DB"
' > "${temporary}/database.dump"

"${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import sys, tarfile
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
    archive.add("/app/data/attachments", arcname=".")
' > "${temporary}/attachments.tar.gz"

(
  cd "${temporary}"
  sha256sum database.dump attachments.tar.gz > SHA256SUMS
)
printf 'created_at_utc=%s\n' "${stamp}" > "${temporary}/MANIFEST"
printf 'compose_project=%s\n' "$("${COMPOSE[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')" >> "${temporary}/MANIFEST"

mv "${temporary}" "${destination}"
"${COMPOSE[@]}" up -d --wait app caddy
services_stopped=false
trap - EXIT
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -name 'dfo-spm-*' -mtime "+${RETENTION_DAYS}" -exec rm -rf -- {} +
printf 'Backup created: %s\n' "${destination}"

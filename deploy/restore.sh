#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
BACKUP_DIR="${1:-}"
LOCK_FILE="${LOCK_FILE:-${ROOT_DIR}/backups/.operations.lock}"
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${ROOT_DIR}/compose.production.yml")

if [[ "${RESTORE_CONFIRM:-}" != "restore" ]]; then
  printf 'Restore is destructive. Re-run with RESTORE_CONFIRM=restore.\n' >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  printf 'Missing production environment file: %s\n' "${ENV_FILE}" >&2
  exit 1
fi
if [[ -z "${BACKUP_DIR}" || ! -d "${BACKUP_DIR}" ]]; then
  printf 'Usage: RESTORE_CONFIRM=restore %s <backup-directory>\n' "$0" >&2
  exit 1
fi
for required in database.dump attachments.tar.gz SHA256SUMS MANIFEST; do
  if [[ ! -f "${BACKUP_DIR}/${required}" ]]; then
    printf 'Backup is missing %s.\n' "${required}" >&2
    exit 1
  fi
done
(
  cd "${BACKUP_DIR}"
  sha256sum --check SHA256SUMS
)

manifest_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=[A-Za-z0-9_.-]+$" "${BACKUP_DIR}/MANIFEST" || true)"
  if [[ -z "${line}" || "${line}" == *$'\n'* ]]; then
    printf 'Backup manifest has an invalid or duplicate %s entry.\n' "${key}" >&2
    exit 1
  fi
  printf '%s\n' "${line#*=}"
}
compose_project="$("${COMPOSE[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
backup_project="$(manifest_value compose_project)"
backup_revision="$(manifest_value schema_revision)"
expected_revision="$("${COMPOSE[@]}" run --rm --no-deps -T app python -c 'from app.main import EXPECTED_SCHEMA_REVISION; print(EXPECTED_SCHEMA_REVISION)')"
if [[ "${backup_project}" != "${compose_project}" ]]; then
  printf 'Backup belongs to a different Compose project.\n' >&2
  exit 1
fi
if [[ "${backup_revision}" != "${expected_revision}" ]]; then
  printf 'Backup schema revision is not supported by this application image.\n' >&2
  exit 1
fi

mkdir -p "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  printf 'Another backup or restore operation is already running.\n' >&2
  exit 1
fi

operation_id="$(date -u +%Y%m%d%H%M%S%N)"
staging_db="spm_restore_${operation_id}"
previous_db="spm_previous_${operation_id}"
failed_db="spm_failed_${operation_id}"
staging_dir=".restore-staging-${operation_id}"
rollback_dir=".restore-rollback-${operation_id}"
services_stopped=false
db_swapped=false
files_swapped=false

"${COMPOSE[@]}" up -d --wait postgres

"${COMPOSE[@]}" exec -T postgres pg_restore --list < "${BACKUP_DIR}/database.dump" >/dev/null
"${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import os, shutil, sys, tarfile
with open(os.devnull, "wb") as sink, tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    for member in archive:
        tarfile.data_filter(member, ".")
        source = archive.extractfile(member) if member.isfile() else None
        if source:
            shutil.copyfileobj(source, sink)
' < "${BACKUP_DIR}/attachments.tar.gz"

cleanup() {
  status=$?
  rollback_failed=false
  if [[ "${status}" -ne 0 ]]; then
    if [[ "${services_stopped}" == true ]]; then
      if [[ "${files_swapped}" == true ]]; then
        if ! "${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import shutil, sys
from pathlib import Path
root = Path("/app/data/attachments")
rollback = root / sys.argv[1]
for child in list(root.iterdir()):
    if child != rollback:
        shutil.rmtree(child) if child.is_dir() else child.unlink()
if rollback.exists():
    for child in list(rollback.iterdir()):
        child.rename(root / child.name)
    rollback.rmdir()
' "${rollback_dir}"; then
          rollback_failed=true
        fi
      fi
      if [[ "${db_swapped}" == true ]]; then
        if ! "${COMPOSE[@]}" exec -T postgres sh -ceu '
          live=$POSTGRES_DB
          previous=$1
          failed=$2
          psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
            --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '\''$live'\'' AND pid <> pg_backend_pid()" >/dev/null
          psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
            --command "ALTER DATABASE \"$live\" RENAME TO \"$failed\""
          if ! psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
            --command "ALTER DATABASE \"$previous\" RENAME TO \"$live\""; then
            psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
              --command "ALTER DATABASE \"$failed\" RENAME TO \"$live\"" || true
            exit 1
          fi
        ' sh "${previous_db}" "${failed_db}"; then
          rollback_failed=true
        fi
      fi
      if [[ "${rollback_failed}" == false ]]; then
        if ! "${COMPOSE[@]}" up -d --wait app caddy >/dev/null; then
          rollback_failed=true
          "${COMPOSE[@]}" stop caddy app >/dev/null 2>&1 || true
        fi
      fi
    fi
    if [[ "${db_swapped}" == false ]]; then
      "${COMPOSE[@]}" exec -T postgres sh -ceu '
        dropdb --force --if-exists --username "$POSTGRES_USER" "$1"
      ' sh "${staging_db}" >/dev/null 2>&1 || true
    fi
    "${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import shutil, sys
from pathlib import Path
target = Path("/app/data/attachments") / sys.argv[1]
if target.exists():
    shutil.rmtree(target)
' "${staging_dir}" >/dev/null 2>&1 || true
    if [[ "${rollback_failed}" == true ]]; then
      printf 'Rollback did not complete. Manual recovery is required; application traffic remains stopped.\n' >&2
      exit 2
    fi
  fi
  exit "${status}"
}
trap cleanup EXIT

"${COMPOSE[@]}" exec -T postgres sh -ceu '
  export PGPASSWORD="$(cat /run/secrets/db_password)"
  staging=$1
  dropdb --force --if-exists --username "$POSTGRES_USER" "$staging"
  createdb --username "$POSTGRES_USER" --owner "$POSTGRES_USER" "$staging"
  pg_restore --exit-on-error --no-owner --no-acl --username "$POSTGRES_USER" --dbname "$staging"
' sh "${staging_db}" < "${BACKUP_DIR}/database.dump"

restored_revision="$("${COMPOSE[@]}" exec -T postgres sh -ceu '
  export PGPASSWORD="$(cat /run/secrets/db_password)"
  psql --username "$POSTGRES_USER" --dbname "$1" --tuples-only --no-align \
    --command "SELECT version_num FROM alembic_version"
' sh "${staging_db}")"
if [[ "${restored_revision}" != "${expected_revision}" ]]; then
  printf 'Restored schema revision does not match the application image.\n' >&2
  exit 1
fi

"${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import shutil, sys, tarfile
from pathlib import Path
root = Path("/app/data/attachments")
target = root / sys.argv[1]
if target.exists():
    shutil.rmtree(target)
target.mkdir(mode=0o700)
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    archive.extractall(target, filter="data")
' "${staging_dir}" < "${BACKUP_DIR}/attachments.tar.gz"

"${COMPOSE[@]}" run --rm --no-deps -T \
  -e SPM_DB_NAME="${staging_db}" \
  -e SPM_ATTACHMENT_DIR="/app/data/attachments/${staging_dir}" \
  app python -m app.verify_attachments

"${COMPOSE[@]}" stop caddy app
services_stopped=true

"${COMPOSE[@]}" exec -T postgres sh -ceu '
  live=$POSTGRES_DB
  staging=$1
  previous=$2
  case "$live" in *[!A-Za-z0-9_]*) printf "Unsafe database name.\n" >&2; exit 1;; esac
  psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
    --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '\''$live'\'' AND pid <> pg_backend_pid()" >/dev/null
  psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
    --command "ALTER DATABASE \"$live\" RENAME TO \"$previous\""
  if ! psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
    --command "ALTER DATABASE \"$staging\" RENAME TO \"$live\""; then
    psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
      --command "ALTER DATABASE \"$previous\" RENAME TO \"$live\""
    exit 1
  fi
' sh "${staging_db}" "${previous_db}"
db_swapped=true

"${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import sys
from pathlib import Path
root = Path("/app/data/attachments")
staging = root / sys.argv[1]
rollback = root / sys.argv[2]
rollback.mkdir(mode=0o700)
originals = []
restored = []
try:
    for child in list(root.iterdir()):
        if child not in {staging, rollback}:
            child.rename(rollback / child.name)
            originals.append(child.name)
    for child in list(staging.iterdir()):
        child.rename(root / child.name)
        restored.append(child.name)
    staging.rmdir()
except Exception:
    for name in restored:
        (root / name).rename(staging / name)
    for name in originals:
        (rollback / name).rename(root / name)
    rollback.rmdir()
    raise
' "${staging_dir}" "${rollback_dir}"
files_swapped=true

"${COMPOSE[@]}" up -d --wait app caddy
services_stopped=false

"${COMPOSE[@]}" exec -T postgres sh -ceu '
  dropdb --force --if-exists --username "$POSTGRES_USER" "$1"
' sh "${previous_db}"
"${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import shutil, sys
from pathlib import Path
target = Path("/app/data/attachments") / sys.argv[1]
if target.exists():
    shutil.rmtree(target)
' "${rollback_dir}"
trap - EXIT
printf 'Restore completed from: %s\n' "${BACKUP_DIR}"

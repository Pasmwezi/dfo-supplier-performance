#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
BACKUP_DIR="${1:-}"
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
for required in database.dump attachments.tar.gz SHA256SUMS; do
  if [[ ! -f "${BACKUP_DIR}/${required}" ]]; then
    printf 'Backup is missing %s.\n' "${required}" >&2
    exit 1
  fi
done
(
  cd "${BACKUP_DIR}"
  sha256sum --check SHA256SUMS
)

"${COMPOSE[@]}" stop caddy app
"${COMPOSE[@]}" up -d --wait postgres

"${COMPOSE[@]}" exec -T postgres sh -ceu '
  export PGPASSWORD="$(cat /run/secrets/db_password)"
  dropdb --force --if-exists --username "$POSTGRES_USER" "$POSTGRES_DB"
  createdb --username "$POSTGRES_USER" --owner "$POSTGRES_USER" "$POSTGRES_DB"
  pg_restore --exit-on-error --no-owner --no-acl --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
' < "${BACKUP_DIR}/database.dump"

"${COMPOSE[@]}" run --rm --no-deps -T app python -c '
import shutil, sys, tarfile
from pathlib import Path
root = Path("/app/data/attachments")
for child in root.iterdir():
    shutil.rmtree(child) if child.is_dir() else child.unlink()
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    archive.extractall(root, filter="data")
' < "${BACKUP_DIR}/attachments.tar.gz"

"${COMPOSE[@]}" up -d --wait app caddy
printf 'Restore completed from: %s\n' "${BACKUP_DIR}"

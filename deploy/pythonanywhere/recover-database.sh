#!/usr/bin/env bash
# Back up and recreate the PythonAnywhere SQLite database when it is corrupted.
set -euo pipefail

APP_DIR="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
DB_PATH="${DB_PATH:-${APP_DIR}/instance/keeptrack_full.db}"
RESET_DATABASE="${RESET_DATABASE:-0}"
PYTHON="${PYTHON:-python}"

cd "${APP_DIR}"
mkdir -p instance/backups

check_integrity() {
  if [[ ! -f "${DB_PATH}" ]]; then
    echo "missing"
    return 2
  fi

  "${PYTHON}" - "${DB_PATH}" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
try:
    connection = sqlite3.connect(path)
    row = connection.execute("PRAGMA integrity_check").fetchone()
    connection.close()
    result = row[0] if row else "no result"
    print(result)
    raise SystemExit(0 if result == "ok" else 1)
except Exception as exc:
    print(exc)
    raise SystemExit(1)
PY
}

if [[ "${RESET_DATABASE}" != "1" ]]; then
  echo "==> Checking SQLite database integrity: ${DB_PATH}"
  if check_integrity; then
    echo "==> Database integrity is OK. No reset needed."
    exit 0
  fi
  echo "==> Database is missing or corrupted. A fresh database will be created."
else
  echo "==> RESET_DATABASE=1 was set. A fresh database will be created."
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_dir="${APP_DIR}/instance/backups/sqlite-${timestamp}"
mkdir -p "${backup_dir}"

if compgen -G "${APP_DIR}/instance/*.db*" >/dev/null; then
  echo "==> Backing up existing SQLite files to ${backup_dir}"
  cp -a "${APP_DIR}"/instance/*.db* "${backup_dir}/"
fi

echo "==> Recreating SQLite database"
FRESH_DATABASE=1 bash deploy/pythonanywhere/init-database.sh "${APP_DIR}"

echo "==> Recovery complete. Reload the PythonAnywhere web app."

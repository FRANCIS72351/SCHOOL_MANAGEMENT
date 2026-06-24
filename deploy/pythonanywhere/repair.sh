#!/usr/bin/env bash
# One-command PythonAnywhere repair for dependency and SQLite startup failures.
set -euo pipefail

APP_DIR="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.11}"

cd "${APP_DIR}"
unset PYTHONHOME PYTHONPATH

echo "==> Installing Python 3.11 user-site dependencies"
bash deploy/pythonanywhere/install-user-site.sh "${APP_DIR}"

if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
  echo ""
  echo "ADMIN_PASSWORD is not set."
  echo "Set it if you need a fresh admin account after database recovery:"
  echo "  export ADMIN_PASSWORD='your_real_admin_password'"
  echo ""
fi

echo "==> Checking/recovering SQLite database"
PYTHON="${PYTHON_BIN}" bash deploy/pythonanywhere/recover-database.sh "${APP_DIR}"

echo ""
echo "==> Repair complete."
echo "PythonAnywhere Web tab settings:"
echo "  Python version: 3.11"
echo "  Virtualenv:     leave blank"
echo "  Source code:    ${APP_DIR}"
echo "  Working dir:    ${APP_DIR}"
echo "Click Reload after saving those settings."

#!/usr/bin/env bash
# Install dependencies into the PythonAnywhere user site when virtualenv is broken.
set -euo pipefail

APP_DIR="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.11}"

unset PYTHONHOME PYTHONPATH
cd "${APP_DIR}"

echo "==> Checking system Python runtime: ${PYTHON_BIN}"
"${PYTHON_BIN}" -c "import subprocess, _posixsubprocess; print('system Python OK')"

echo "==> Checking pip"
if ! "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1; then
  echo "pip is not available for ${PYTHON_BIN}. Ask PythonAnywhere support to enable pip for Python 3.11." >&2
  exit 1
fi

echo "==> Installing requirements into user site-packages"
"${PYTHON_BIN}" -m pip install --user -r requirements.txt

echo "==> Verifying required imports"
"${PYTHON_BIN}" -c "import flask, pyotp, sqlalchemy; print('imports OK')"

cat <<'MSG'

==> User-site install complete.
PythonAnywhere Web tab:
  - Python version: 3.11
  - Virtualenv field: leave blank
  - Source code: /home/YOUR_USERNAME/SCHOOL_MANAGEMENT
  - Working directory: /home/YOUR_USERNAME/SCHOOL_MANAGEMENT
Then click Reload.
MSG

#!/usr/bin/env bash
# Diagnose PythonAnywhere virtualenv/runtime issues before installing packages.
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
VENV_NAME="${VENV_NAME:-schoolmgmt}"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python${PYTHON_VERSION}}"
VENV_PYTHON="${HOME}/.virtualenvs/${VENV_NAME}/bin/python"

echo "==> Environment"
echo "USER=${USER:-}"
echo "HOME=${HOME}"
echo "PYTHONHOME=${PYTHONHOME:-}"
echo "PYTHONPATH=${PYTHONPATH:-}"
echo "PATH=${PATH}"
echo ""

check_python() {
  local label="$1"
  local python_bin="$2"

  echo "==> ${label}: ${python_bin}"
  if [[ ! -x "${python_bin}" ]]; then
    echo "missing"
    echo ""
    return
  fi

  "${python_bin}" - <<'PY' || true
import importlib.util
import os
import sys
import sysconfig

print(f"executable={sys.executable}")
print(f"version={sys.version.split()[0]}")
print(f"prefix={sys.prefix}")
print(f"base_prefix={sys.base_prefix}")
print(f"stdlib={sysconfig.get_path('stdlib')}")
print(f"platstdlib={sysconfig.get_path('platstdlib')}")
print(f"PYTHONHOME={os.environ.get('PYTHONHOME', '')}")
print(f"PYTHONPATH={os.environ.get('PYTHONPATH', '')}")
print(f"_posixsubprocess_spec={importlib.util.find_spec('_posixsubprocess')}")
try:
    import subprocess
    import _posixsubprocess
except Exception as exc:
    print(f"runtime_check=FAILED: {exc!r}")
else:
    print("runtime_check=OK")
PY
  echo ""
}

check_python "system Python" "${SYSTEM_PYTHON}"
check_python "virtualenv Python" "${VENV_PYTHON}"

echo "==> Next step"
echo "If system Python runtime_check=FAILED, PythonAnywhere must fix/switch the Python ${PYTHON_VERSION} runtime before pip can work."
echo "If system Python is OK but virtualenv Python failed, remove and recreate the ${VENV_NAME} virtualenv."

#!/usr/bin/env python3
"""Deploy this Flask app to PythonAnywhere using SSH plus the webapp API.

Required environment variables:
  PYTHONANYWHERE_USERNAME
  PYTHONANYWHERE_API_TOKEN

Optional environment variables:
  PYTHONANYWHERE_DOMAIN
  PYTHONANYWHERE_HOST
  PYTHONANYWHERE_REPO_URL
  PYTHONANYWHERE_BRANCH
  PYTHONANYWHERE_PROJECT_DIR
  PYTHONANYWHERE_ADMIN_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_REPO_URL = "https://github.com/FRANCIS72351/SCHOOL_MANAGEMENT.git"


class ApiError(RuntimeError):
    """Raised when PythonAnywhere returns an unsuccessful API response."""


@dataclass(frozen=True)
class DeployConfig:
    username: str
    token: str
    host: str
    domain: str
    repo_url: str
    branch: str
    project_dir: str
    python_version: str
    virtualenv_path: str
    venv_name: str
    ssh_host: str
    ssh_user: str
    admin_password: str
    admin_email: str
    admin_name: str
    fresh_database: bool
    skip_ssh: bool
    reload_only: bool
    dry_run: bool


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def current_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else "main"
    except (OSError, subprocess.CalledProcessError):
        return "main"


def parse_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def build_config(argv: list[str] | None = None) -> DeployConfig:
    parser = argparse.ArgumentParser(description="Deploy SCHOOL_MANAGEMENT to PythonAnywhere.")
    parser.add_argument("--username", default=env("PYTHONANYWHERE_USERNAME"))
    parser.add_argument("--api-token", default=env("PYTHONANYWHERE_API_TOKEN"))
    parser.add_argument("--host", default=env("PYTHONANYWHERE_HOST", "www.pythonanywhere.com"))
    parser.add_argument("--domain", default=env("PYTHONANYWHERE_DOMAIN"))
    parser.add_argument("--repo-url", default=env("PYTHONANYWHERE_REPO_URL", DEFAULT_REPO_URL))
    parser.add_argument("--branch", default=env("PYTHONANYWHERE_BRANCH", current_branch()))
    parser.add_argument("--project-dir", default=env("PYTHONANYWHERE_PROJECT_DIR"))
    parser.add_argument("--python-version", default=env("PYTHONANYWHERE_PYTHON_VERSION", "3.11"))
    parser.add_argument("--virtualenv-path", default=env("PYTHONANYWHERE_VIRTUALENV"))
    parser.add_argument("--venv-name", default=env("PYTHONANYWHERE_VENV_NAME", "schoolmgmt"))
    parser.add_argument("--ssh-host", default=env("PYTHONANYWHERE_SSH_HOST", "ssh.pythonanywhere.com"))
    parser.add_argument("--ssh-user", default=env("PYTHONANYWHERE_SSH_USER"))
    parser.add_argument("--admin-password", default=env("PYTHONANYWHERE_ADMIN_PASSWORD"))
    parser.add_argument("--admin-email", default=env("PYTHONANYWHERE_ADMIN_EMAIL", "admin@school.com"))
    parser.add_argument("--admin-name", default=env("PYTHONANYWHERE_ADMIN_NAME", "School Administrator"))
    parser.add_argument("--fresh-database", action="store_true", default=parse_bool(env("PYTHONANYWHERE_FRESH_DATABASE")))
    parser.add_argument("--skip-ssh", action="store_true", default=parse_bool(env("PYTHONANYWHERE_SKIP_SSH")))
    parser.add_argument("--reload-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    username = args.username
    domain = args.domain or (f"{username}.pythonanywhere.com" if username else "")
    project_dir = args.project_dir or (f"/home/{username}/SCHOOL_MANAGEMENT" if username else "")
    virtualenv_path = args.virtualenv_path or (f"/home/{username}/.virtualenvs/{args.venv_name}" if username else "")
    ssh_user = args.ssh_user or username

    missing = []
    if not username:
        missing.append("PYTHONANYWHERE_USERNAME")
    if not args.api_token:
        missing.append("PYTHONANYWHERE_API_TOKEN")
    if missing and not args.dry_run:
        parser.error("Missing required setting(s): " + ", ".join(missing))

    return DeployConfig(
        username=username or "YOUR_USERNAME",
        token=args.api_token or "DRY_RUN_TOKEN",
        host=args.host,
        domain=domain or "YOUR_USERNAME.pythonanywhere.com",
        repo_url=args.repo_url,
        branch=args.branch,
        project_dir=project_dir or "/home/YOUR_USERNAME/SCHOOL_MANAGEMENT",
        python_version=args.python_version,
        virtualenv_path=virtualenv_path or "/home/YOUR_USERNAME/.virtualenvs/schoolmgmt",
        venv_name=args.venv_name,
        ssh_host=args.ssh_host,
        ssh_user=ssh_user or "YOUR_USERNAME",
        admin_password=args.admin_password,
        admin_email=args.admin_email,
        admin_name=args.admin_name,
        fresh_database=args.fresh_database,
        skip_ssh=args.skip_ssh,
        reload_only=args.reload_only,
        dry_run=args.dry_run,
    )


class PythonAnywhereApi:
    def __init__(self, config: DeployConfig) -> None:
        self.config = config
        self.base_url = f"https://{config.host}/api/v0/user/{config.username}"
        self.headers = {"Authorization": f"Token {config.token}"}

    def request(self, method: str, path: str, data: dict[str, str] | None = None) -> object:
        encoded = None
        headers = dict(self.headers)
        if data is not None:
            encoded = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise ApiError(f"{method} {path} failed with HTTP {exc.code}: {body}") from exc

    def upload_file(self, remote_path: str, content: str) -> None:
        boundary = "----pythonanywhere-deploy-" + secrets.token_hex(8)
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="content"; filename="wsgi.py"\r\n'
            "Content-Type: text/x-python\r\n\r\n"
            f"{content}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        quoted_path = urllib.parse.quote(remote_path)
        request = urllib.request.Request(
            f"{self.base_url}/files/path{quoted_path}",
            data=body,
            headers={
                **self.headers,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise ApiError(f"Upload {remote_path} failed with HTTP {exc.code}: {body}") from exc


def pythonanywhere_version(version: str) -> str:
    digits = "".join(part for part in version.split(".") if part.isdigit())
    if version.startswith("python"):
        return version
    return f"python{digits}"


def wsgi_path(domain: str) -> str:
    return f"/var/www/{domain.replace('.', '_')}_wsgi.py"


def wsgi_content(config: DeployConfig) -> str:
    return f'''import os
import sys

PROJECT_HOME = {config.project_dir!r}

if PROJECT_HOME not in sys.path:
    sys.path.insert(0, PROJECT_HOME)

os.chdir(PROJECT_HOME)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_HOME, ".env"))
except ImportError:
    pass

from wsgi import application
'''


def run_ssh_setup(config: DeployConfig) -> None:
    if config.skip_ssh or config.reload_only:
        print("==> Skipping SSH code/dependency setup")
        return

    remote_script = f"""set -euo pipefail
PROJECT_DIR={shlex.quote(config.project_dir)}
REPO_URL={shlex.quote(config.repo_url)}
BRANCH={shlex.quote(config.branch)}
PYTHON_VERSION={shlex.quote(config.python_version)}
VENV_NAME={shlex.quote(config.venv_name)}
VIRTUALENV_PATH={shlex.quote(config.virtualenv_path)}
ADMIN_PASSWORD={shlex.quote(config.admin_password)}
ADMIN_EMAIL={shlex.quote(config.admin_email)}
ADMIN_NAME={shlex.quote(config.admin_name)}
FRESH_DATABASE={"1" if config.fresh_database else "0"}

echo "==> Syncing $REPO_URL branch $BRANCH into $PROJECT_DIR"
if [[ -d "$PROJECT_DIR/.git" ]]; then
  cd "$PROJECT_DIR"
  git fetch origin "$BRANCH"
  git checkout -B "$BRANCH" "origin/$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$PROJECT_DIR"
  cd "$PROJECT_DIR"
fi

if ! command -v mkvirtualenv >/dev/null 2>&1; then
  echo "mkvirtualenv was not found. Create a Manual PythonAnywhere web app first, then rerun deploy." >&2
  exit 1
fi

unset PYTHONHOME PYTHONPATH
PYTHON_BIN="/usr/bin/python$PYTHON_VERSION"
if ! "$PYTHON_BIN" -c "import subprocess, _posixsubprocess" >/dev/null 2>&1; then
  echo "$PYTHON_BIN cannot import _posixsubprocess on this PythonAnywhere account." >&2
  echo "Dependency installation cannot run until Python $PYTHON_VERSION is fixed on the account/system image." >&2
  echo "Unset PYTHONHOME/PYTHONPATH and retry; if this still fails, switch the PythonAnywhere system image or contact support." >&2
  exit 1
fi

if [[ ! -d "$VIRTUALENV_PATH" ]]; then
  mkvirtualenv --python="$PYTHON_BIN" "$VENV_NAME"
fi

# shellcheck disable=SC1090
source "$VIRTUALENV_PATH/bin/activate"
unset PYTHONHOME PYTHONPATH
if ! python -c "import subprocess, _posixsubprocess" >/dev/null 2>&1; then
  echo "Python $PYTHON_VERSION on this PythonAnywhere account cannot import _posixsubprocess." >&2
  echo "Remove the broken virtualenv, unset PYTHONHOME/PYTHONPATH, then recreate it with /usr/bin/python$PYTHON_VERSION." >&2
  echo "If it still fails, switch the Web tab/system image to one that supports Python $PYTHON_VERSION, or contact PythonAnywhere support." >&2
  exit 1
fi
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

mkdir -p instance static/uploads
export ADMIN_EMAIL ADMIN_NAME ADMIN_PASSWORD
if [[ ! -f .env ]]; then
  python - <<'PY'
import os
import secrets
import shlex
from pathlib import Path

values = {{
    "SECRET_KEY": secrets.token_hex(32),
    "FLASK_ENV": "production",
    "PRODUCTION": "1",
    "SESSION_COOKIE_SECURE": "true",
    "WTF_CSRF_SSL_STRICT": "true",
    "FRESH_DATABASE": "0",
    "ADMIN_EMAIL": os.environ["ADMIN_EMAIL"],
    "ADMIN_NAME": os.environ["ADMIN_NAME"],
}}
Path(".env").write_text("\\n".join(f"{{key}}={{shlex.quote(value)}}" for key, value in values.items()) + "\\n")
PY
  chmod 600 .env
fi

if [[ -n "$ADMIN_PASSWORD" ]]; then
  python - <<'PY'
import os
import shlex
from pathlib import Path

env_path = Path(".env")
updates = {{
    "ADMIN_EMAIL": os.environ["ADMIN_EMAIL"],
    "ADMIN_NAME": os.environ["ADMIN_NAME"],
    "ADMIN_PASSWORD": os.environ["ADMIN_PASSWORD"],
}}
lines = env_path.read_text().splitlines() if env_path.exists() else []
seen = set()
next_lines = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
    if key in updates:
        next_lines.append(f"{{key}}={{shlex.quote(updates[key])}}")
        seen.add(key)
    else:
        next_lines.append(line)
for key, value in updates.items():
    if key not in seen:
        next_lines.append(f"{{key}}={{shlex.quote(value)}}")
env_path.write_text("\\n".join(next_lines) + "\\n")
PY
fi

FRESH_DATABASE="$FRESH_DATABASE" bash deploy/pythonanywhere/init-database.sh "$PROJECT_DIR"
echo "==> Remote code, dependencies, and database are ready"
"""
    if config.dry_run:
        print("==> Would run SSH setup on " + f"{config.ssh_user}@{config.ssh_host}")
        return

    subprocess.run(
        ["ssh", f"{config.ssh_user}@{config.ssh_host}", "bash", "-s"],
        input=remote_script,
        text=True,
        check=True,
    )


def ensure_webapp(config: DeployConfig) -> None:
    api = PythonAnywhereApi(config)
    api_python_version = pythonanywhere_version(config.python_version)
    site_path = f"/webapps/{config.domain}/"
    static_path = f"/webapps/{config.domain}/static_files/"
    reload_path = f"/webapps/{config.domain}/reload/"
    wsgi_remote_path = wsgi_path(config.domain)

    if config.reload_only:
        if config.dry_run:
            print(f"==> Would reload https://{config.domain}")
            return
        print("==> Reloading webapp")
        api.request("POST", reload_path)
        print(f"==> Reloaded: https://{config.domain}")
        return

    if config.dry_run:
        print(f"==> Would create/update webapp: {config.domain}")
        print(f"    source_directory={config.project_dir}")
        print(f"    virtualenv_path={config.virtualenv_path}")
        print(f"    python_version={api_python_version}")
        print(f"==> Would upload WSGI file: {wsgi_remote_path}")
        print(f"==> Would map /static/ to {config.project_dir}/static/")
        print(f"==> Would reload https://{config.domain}")
        return

    try:
        api.request("GET", site_path)
        print(f"==> Updating existing webapp {config.domain}")
        api.request(
            "PATCH",
            site_path,
            {
                "python_version": api_python_version,
                "source_directory": config.project_dir,
                "virtualenv_path": config.virtualenv_path,
                "force_https": "true",
            },
        )
    except ApiError as exc:
        if "HTTP 404" not in str(exc):
            raise
        print(f"==> Creating webapp {config.domain}")
        api.request(
            "POST",
            "/webapps/",
            {"domain_name": config.domain, "python_version": api_python_version},
        )
        api.request(
            "PATCH",
            site_path,
            {
                "source_directory": config.project_dir,
                "virtualenv_path": config.virtualenv_path,
                "force_https": "true",
            },
        )

    print(f"==> Uploading WSGI file to {wsgi_remote_path}")
    api.upload_file(wsgi_remote_path, wsgi_content(config))

    print("==> Ensuring static file mapping")
    static_mappings = api.request("GET", static_path)
    static_url = "/static/"
    static_dir = f"{config.project_dir}/static/"
    mapping = next(
        (item for item in static_mappings if item.get("url") == static_url),
        None,
    )
    if mapping:
        if mapping.get("path") != static_dir:
            api.request("PATCH", f"{static_path}{mapping['id']}/", {"url": static_url, "path": static_dir})
    else:
        api.request("POST", static_path, {"url": static_url, "path": static_dir})

    print("==> Reloading webapp")
    api.request("POST", reload_path)
    print(f"==> Deployed: https://{config.domain}")


def main(argv: list[str] | None = None) -> int:
    config = build_config(argv)
    run_ssh_setup(config)
    ensure_webapp(config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

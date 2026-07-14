# Deploy on PythonAnywhere

Host the School Management System at `https://YOUR_USERNAME.pythonanywhere.com`.

**Repository:** https://github.com/FRANCIS72351/SCHOOL_MANAGEMENT.git

Your local `instance/*.db` is not in git. PythonAnywhere creates its own SQLite database and keeps it on redeploys unless `FRESH_DATABASE=1` is explicitly set.

---

## Direct deploy from a terminal

Use this when you have a PythonAnywhere API token and SSH access configured:

```bash
export PYTHONANYWHERE_USERNAME=YOUR_USERNAME
export PYTHONANYWHERE_API_TOKEN=YOUR_API_TOKEN
export PYTHONANYWHERE_ADMIN_PASSWORD='set_a_strong_password_here'
python3 deploy/pythonanywhere/deploy.py --branch main
```

Optional settings:

| Variable | Default |
|----------|---------|
| `PYTHONANYWHERE_DOMAIN` | `YOUR_USERNAME.pythonanywhere.com` |
| `PYTHONANYWHERE_HOST` | `www.pythonanywhere.com` (`eu.pythonanywhere.com` for EU accounts) |
| `PYTHONANYWHERE_REPO_URL` | `https://github.com/FRANCIS72351/SCHOOL_MANAGEMENT.git` |
| `PYTHONANYWHERE_PROJECT_DIR` | `/home/YOUR_USERNAME/SCHOOL_MANAGEMENT` |
| `PYTHONANYWHERE_PYTHON_VERSION` | `3.11` |
| `PYTHONANYWHERE_VENV_NAME` | `schoolmgmt` |
| `PYTHONANYWHERE_FRESH_DATABASE` | `0`; set `1` only to wipe production SQLite data |

The deploy command syncs code over SSH, installs dependencies, initializes tables, creates or updates the PythonAnywhere web app through the API, configures `/static/`, uploads the WSGI file, and reloads the site.

To update only the web app configuration and reload after code is already on PythonAnywhere:

```bash
python3 deploy/pythonanywhere/deploy.py --skip-ssh
```

---

## Manual deploy

### 1. Open PythonAnywhere

1. Sign in at [pythonanywhere.com](https://www.pythonanywhere.com)
2. Open the **Dashboard**

---

### 2. Clone and install (Bash console)

Open a **Bash** console and run:

```bash
cd ~
git clone https://github.com/FRANCIS72351/SCHOOL_MANAGEMENT.git
cd SCHOOL_MANAGEMENT
bash deploy/pythonanywhere/setup.sh
```

The virtualenv path is:

```bash
/home/YOUR_USERNAME/.virtualenvs/schoolmgmt
```

To create it manually with Python 3.11 and download dependencies:

```bash
cd ~/SCHOOL_MANAGEMENT
mkvirtualenv --python=/usr/bin/python3.11 schoolmgmt
workon schoolmgmt
python --version
pip install --upgrade pip
pip install -r requirements.txt
```

Edit `.env` and set a strong admin password:

```bash
nano ~/SCHOOL_MANAGEMENT/.env
```

Set at minimum:

```
ADMIN_PASSWORD=your_secure_password_here
```

Re-run database + admin setup if you changed the password after first run:

```bash
cd ~/SCHOOL_MANAGEMENT
workon schoolmgmt
bash deploy/pythonanywhere/init-database.sh
```

---

### 3. Create the Web app

Go to the **Web** tab → **Add a new web app**:

| Setting | Value |
|---------|--------|
| Framework | Manual configuration |
| Python | 3.11 |

Then configure:

| Field | Value |
|-------|--------|
| **Source code** | `/home/YOUR_USERNAME/SCHOOL_MANAGEMENT` |
| **Working directory** | `/home/YOUR_USERNAME/SCHOOL_MANAGEMENT` |
| **Virtualenv** | `/home/YOUR_USERNAME/.virtualenvs/schoolmgmt` |

Replace `YOUR_USERNAME` with your PythonAnywhere username.

---

### 4. WSGI configuration

Click the WSGI configuration file link and replace its contents with
`deploy/pythonanywhere/wsgi.py` from the project, changing:

```python
USERNAME = "YOUR_USERNAME"
```

to your real username, e.g.:

```python
USERNAME = "francis72351"
```

Save the file.

---

### 5. Static files

On the **Web** tab, under **Static files**, add:

| URL | Directory |
|-----|-----------|
| `/static/` | `/home/YOUR_USERNAME/SCHOOL_MANAGEMENT/static/` |

---

### 6. Go live

Click the green **Reload** button on the Web tab.

Visit: `https://YOUR_USERNAME.pythonanywhere.com`

Log in with the admin email/password from `.env` (default email: `admin@school.com`).

---

## Updates (after you push to GitHub)

```bash
cd ~/SCHOOL_MANAGEMENT
git pull origin main
workon schoolmgmt
pip install -r requirements.txt
```

Then **Reload** the web app on the Web tab.

This keeps your PythonAnywhere database; it does not wipe it.

---

## Optional: copy your local database to PythonAnywhere

Only if you want your Windows dev data on PythonAnywhere:

```bash
# From your Windows machine (Git Bash or PowerShell with scp)
scp instance/keeptrack_full.db YOUR_USERNAME@ssh.pythonanywhere.com:/home/YOUR_USERNAME/SCHOOL_MANAGEMENT/instance/
```

Then reload the web app.

---

## Troubleshooting

### One-command repair

Use this when the error log shows missing packages such as `pyotp` and/or `sqlite3.DatabaseError: database disk image is malformed`:

```bash
cd ~/SCHOOL_MANAGEMENT
git pull origin cursor/pythonanywhere-direct-deploy-a54b
export ADMIN_EMAIL=admin@school.com
export ADMIN_PASSWORD='set_a_strong_password_here'
bash deploy/pythonanywhere/repair.sh
```

Then set PythonAnywhere Web tab **Virtualenv** to blank, set Python version to 3.11, and click **Reload**.

PythonAnywhere error logs keep old entries. After reload, check only new lines with the current date/time.

**502 / import error**

- Check **Error log** on the Web tab
- If using the no-virtualenv fallback, keep the Web tab Virtualenv field blank and verify with `/usr/bin/python3.11 -c "import pyotp; print('pyotp OK')"`
- If using a virtualenv, confirm `workon schoolmgmt` has all packages: `python -m pip install -r requirements.txt`

For `ModuleNotFoundError: No module named 'pyotp'` with the blank-virtualenv fallback, install user-site dependencies:

```bash
cd ~/SCHOOL_MANAGEMENT
bash deploy/pythonanywhere/install-user-site.sh
/usr/bin/python3.11 -c "import pyotp; print('pyotp OK')"
```

**`ModuleNotFoundError: No module named '_posixsubprocess'` while running `pip`**

This means the selected Python 3.11 runtime on PythonAnywhere is not loading its standard compiled modules correctly. Recreate the virtualenv after clearing Python path overrides:

```bash
cd ~/SCHOOL_MANAGEMENT
deactivate 2>/dev/null || true
rmvirtualenv schoolmgmt
unset PYTHONHOME PYTHONPATH
/usr/bin/python3.11 -c "import subprocess, _posixsubprocess; print('Python 3.11 OK')"
mkvirtualenv --python=/usr/bin/python3.11 schoolmgmt
workon schoolmgmt
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `/usr/bin/python3.11 -c "import subprocess, _posixsubprocess"` fails before the virtualenv is created, stop there: pip cannot work because Python 3.11 itself is broken on that account/system image. Switch the PythonAnywhere Web tab/system image to one that supports Python 3.11, or contact PythonAnywhere support and ask them to fix Python 3.11 missing `_posixsubprocess`. The virtualenv path remains `/home/YOUR_USERNAME/.virtualenvs/schoolmgmt`.

If system Python prints `Python 3.11 OK` but the virtualenv still fails, clear Python path contamination after activation:

```bash
workon schoolmgmt
unset PYTHONHOME PYTHONPATH
python -c "import sys; print(sys.executable); print(sys.path)"
python -c "import subprocess, _posixsubprocess; print('venv Python OK')"
```

If this fixes it, remove any `PYTHONHOME` or `PYTHONPATH` exports from `~/.bashrc`, `~/.profile`, and `~/.virtualenvs/schoolmgmt/bin/postactivate`.

If system Python works but every virtualenv fails, install dependencies into the PythonAnywhere user site and leave the Web tab **Virtualenv** field blank:

```bash
cd ~/SCHOOL_MANAGEMENT
git pull origin cursor/pythonanywhere-direct-deploy-a54b
bash deploy/pythonanywhere/install-user-site.sh
```

Then set the Web tab to Python 3.11, clear the Virtualenv field, and reload the app.

To print a full diagnosis before contacting support:

```bash
cd ~/SCHOOL_MANAGEMENT
bash deploy/pythonanywhere/doctor.sh
```

**SECRET_KEY error**

- Ensure `.env` exists with `SECRET_KEY` and `PRODUCTION=1`
- WSGI file must `load_dotenv` (see `deploy/pythonanywhere/wsgi.py`)

**CSRF / login issues**

- PythonAnywhere uses HTTPS; keep `SESSION_COOKIE_SECURE=true` in `.env`

**Database permission errors**

```bash
chmod 775 ~/SCHOOL_MANAGEMENT/instance
```

**`sqlite3.DatabaseError: database disk image is malformed`**

The SQLite database is corrupted. Back it up, recreate a fresh database, then reload the Web app:

```bash
cd ~/SCHOOL_MANAGEMENT
workon schoolmgmt
export ADMIN_EMAIL=admin@school.com
export ADMIN_PASSWORD='set_a_strong_password_here'
bash deploy/pythonanywhere/recover-database.sh
```

The script saves old SQLite files under `instance/backups/` before creating the fresh database. To reset even when integrity checks pass, run `RESET_DATABASE=1 bash deploy/pythonanywhere/recover-database.sh`.

**OCR grading (pytesseract)**

- Optional feature; may not work on PythonAnywhere free tier (no Tesseract). Other features work normally.

---

## PythonAnywhere vs VPS

| | PythonAnywhere | Linux VPS |
|--|----------------|-----------|
| Server | Managed WSGI | Gunicorn + Nginx |
| Setup | `deploy/pythonanywhere/setup.sh` | `deploy/install-linux.sh` |
| Domain | `username.pythonanywhere.com` | Your own domain |
| Database | Fresh SQLite in `instance/` | Fresh SQLite in `instance/` |

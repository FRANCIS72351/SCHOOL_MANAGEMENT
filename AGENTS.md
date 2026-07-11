# AGENTS.md

## Cursor Cloud specific instructions

This is a single Flask app ("SOFTNETAFRICA"/KeepTrack school management system), Python 3.12, SQLite by default (`instance/keeptrack_full.db`). The main entrypoint is `app.py` (a large monolith); models in `models.py`.

Dependencies are installed into a local virtualenv at `.venv` (see the startup update script). Use `.venv/bin/python` / `.venv/bin/pip` for all commands.

### Running the app (dev)
- Run with the app's own entrypoint: `PORT=5000 BIND_HOST=0.0.0.0 .venv/bin/python app.py` (serves via Waitress; honors `PORT`/`BIND_HOST`).
- Non-obvious gotcha: `flask run` (as documented in `README.md`) does NOT work here because the repo root contains an empty `__init__.py`, which makes Flask's CLI treat `/workspace` as a package and import `workspace.app`, breaking `from models import ...`. Use `python app.py` instead. For the auto-reloading Flask dev server, use `.venv/bin/python -c "from app import app; app.run(host='0.0.0.0', port=5000, debug=True)"`.
- `.env` is gitignored and must be configured for local dev. `.env.example` defaults to production (`FLASK_ENV=production`, `SESSION_COOKIE_SECURE=true`), which enforces a real `SECRET_KEY` and secure cookies that break login over plain HTTP. For local dev set `FLASK_ENV=development`, `PRODUCTION=0`, and `SESSION_COOKIE_SECURE=false`.

### Database / seed data
- `.venv/bin/python seed_data.py` drops and recreates all tables and seeds demo accounts:
  - admin@example.com / adminpass, teacher@example.com / teacherpass, student@example.com / studentpass (plus parent/sponsor).
- `init_db.py` only creates tables + applies legacy SQLite column patches (non-destructive).

### Tests
- Run with `.venv/bin/python -m pytest -q` (unittest-style `test_*.py`, config in `pytest.ini`).
- Pre-existing test-isolation issue (NOT an environment problem): the full-suite run fails `test_academic_rollover.py::AcademicRolloverTestCase::test_wizard_rollover_posts_per_class_registration_income` because tests share the same SQLite DB and pollute state. It passes when that file is run alone (`.venv/bin/python -m pytest test_academic_rollover.py`).

### Lint / compile
- No dedicated linter is configured. CI (`.github/workflows/ci.yml`) only does `python -m py_compile app.py export_routes.py` then `pytest`.

### System packages (already in the VM snapshot)
- `tesseract-ocr` and `libzbar0` enable optional OCR / QR-barcode scanning features. The app degrades gracefully (feature-flagged) when they are absent. `python3.12-venv` is required to create `.venv`.

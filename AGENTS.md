# AGENTS.md

## Cursor Cloud specific instructions

This is a single-service Flask app (KeepTrack / SOFTNETAFRICA school management system).
There is no frontend build step; templates are server-rendered Jinja under `templates/`.

### Environment
- Python 3.12. Dependencies are installed into a `.venv` virtualenv by the startup update script. Prefix commands with `.venv/bin/` (e.g. `.venv/bin/python`, `.venv/bin/pytest`).
- `Werkzeug` must stay `<2.3`: `requirements.txt` does not pin it, but `pip install -r requirements.txt` resolves to Werkzeug 3.x which breaks the pinned Flask 2.2.5 / Flask-WTF 1.1.1 (`werkzeug.urls.url_encode` was removed). The update script reinstalls `Werkzeug<2.3` after requirements; keep that constraint.
- The app runs as development by default (no `.env` needed); it falls back to a SQLite DB at `instance/keeptrack_full.db` and a default dev secret. Do NOT set `FLASK_ENV=production`/`PRODUCTION=1` for local dev — production mode requires a real `SECRET_KEY` and forces secure cookies.

### Database (not in update script — run manually when needed)
- Seed/reset dev data: `.venv/bin/python seed_data.py`. WARNING: this calls `db.drop_all()` then recreates and seeds. Seeded logins: `admin@example.com/adminpass`, `teacher@example.com/teacherpass`, `student@example.com/studentpass`.
- Create empty tables without wiping: `.venv/bin/python create_tables.py`. Legacy schema patches: `.venv/bin/python init_db.py`.

### Run / test / lint
- Run dev server: `.venv/bin/python -m flask run --host=127.0.0.1 --port=5000` (login at `/login`, dashboard at `/dashboard`).
- Tests: `.venv/bin/python -m pytest -q` (see `pytest.ini`).
- "Lint"/compile check (matches CI in `.github/workflows/ci.yml`): `.venv/bin/python -m py_compile app.py export_routes.py`.

### Known caveats
- 4 tests in `test_academic_rollover.py` fail due to a pre-existing app bug (`klass.grade_level` is a string, so `klass.grade_level + 1` raises `TypeError` in `app.py`). This is unrelated to environment setup; the other 11 tests pass.
- `pytesseract`/Tesseract OCR binary is not installed; OCR-related routes degrade gracefully (`ocr_scanner.py` guards for missing libs) and are not needed for core flows.

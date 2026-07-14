# AGENTS.md

## Cursor Cloud specific instructions

Single service: a Flask school-management web app (SOFTNETAFRICA / KeepTrack) backed by SQLite. Standard setup/run steps live in `README.md`; only the non-obvious caveats are noted here.

- Python deps live in a project virtualenv at `.venv` (created by the startup update script). Always invoke tools through it, e.g. `.venv/bin/python`, `.venv/bin/pytest`. The DB is SQLite at `instance/keeptrack_full.db` (gitignored, auto-created on app import).
- Run the app with `PORT=5000 BIND_HOST=127.0.0.1 .venv/bin/python app.py` (serves via Waitress; falls back to `app.run`). Do NOT use `flask run` — it fails with `ModuleNotFoundError: No module named 'models'` because `app.py` only adds its dir to `sys.path` after the top-level `from models import ...`.
- Seed/reset dev data: `.venv/bin/python seed_data.py`. This DROPS and recreates all tables, then creates logins: `admin@example.com/adminpass`, `teacher@example.com/teacherpass`, `student@example.com/studentpass` (plus parent/sponsor).
- Tests: `.venv/bin/python -m pytest -q`. The 4 tests in `test_academic_rollover.py` fail due to a pre-existing app bug at `app.py:8718` (`klass.grade_level + 1` when `grade_level` is a string), NOT an environment problem. The other test files pass.
- `Werkzeug` must stay `<2.3` (pinned to `2.2.3`): `Flask-WTF 1.1.1` imports `werkzeug.urls.url_encode`, which was removed in Werkzeug 2.3. Leaving it unpinned installs Werkzeug 3.x and breaks every import of `app`.
- OCR (`pytesseract`) is optional; the system `tesseract` binary is not installed and the code degrades gracefully without it.

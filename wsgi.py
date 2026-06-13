"""
WSGI entry point for Gunicorn / production servers on Linux.

  gunicorn -c gunicorn.conf.py wsgi:application

Or with Waitress (Windows-friendly, also works on Linux):

  waitress-serve --host=127.0.0.1 --port=8000 --threads=8 wsgi:application
"""
from app import app as application

# Gunicorn looks for `application`; some hosts expect `app`.
app = application

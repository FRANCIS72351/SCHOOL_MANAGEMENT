# Paste this into your PythonAnywhere WSGI file
# (Web tab → WSGI configuration file, usually /var/www/YOURUSERNAME_pythonanywhere_com_wsgi.py)
#
# Replace YOUR_USERNAME with your PythonAnywhere username (e.g. francis72351).

import os
import site
import sys

USERNAME = "YOUR_USERNAME"
PROJECT_HOME = f"/home/{USERNAME}/SCHOOL_MANAGEMENT"

USER_SITE = site.getusersitepackages()
if USER_SITE not in sys.path:
    sys.path.insert(0, USER_SITE)

if PROJECT_HOME not in sys.path:
    sys.path.insert(0, PROJECT_HOME)

os.chdir(PROJECT_HOME)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_HOME, ".env"))
except ImportError:
    pass

from wsgi import application

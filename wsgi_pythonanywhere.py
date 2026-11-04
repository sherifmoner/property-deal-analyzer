# =====================================================================
# PythonAnywhere WSGI entry — TEMPLATE (v3.6)
#
# PythonAnywhere does NOT use this file in place. Copy its contents into
# the WSGI configuration file PythonAnywhere generates for the web app
# (Web tab -> "WSGI configuration file" link, path like
# /var/www/<username>_pythonanywhere_com_wsgi.py), replacing the two
# placeholders below. Full procedure: PA_DEPLOY.md in this repo.
# =====================================================================

import os
import sys

# --- REPLACE with the PythonAnywhere account username -----------------
USERNAME = "YOUR_USERNAME"
# ----------------------------------------------------------------------

project_home = f"/home/{USERNAME}/property-deal-analyzer"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# The live database lives INSIDE the project tree on PythonAnywhere's
# persistent disk (see PA_DEPLOY.md: code updates pull code only and must
# never overwrite analyzer.db). Pin its path explicitly so the app never
# falls back to a different working-directory copy.
os.environ.setdefault("PDA_DB", os.path.join(project_home, "analyzer.db"))

# Optional: a free Census API key unlocks the direct api.census.gov
# demographics fallback at full rate (the app also works keyless-first;
# Census Reporter remains the primary source where reachable).
# Set it here OR in the app's settings store — never commit a real key.
# os.environ.setdefault("CENSUS_API_KEY", "")

from app import app as application  # noqa: E402  (path setup must come first)

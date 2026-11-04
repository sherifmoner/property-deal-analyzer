# Deploying the Property Deal Analyzer on PythonAnywhere (free tier)

Permanent home for the app: `https://<username>.pythonanywhere.com`
(always-on, stable URL, persistent disk — saved properties and scenarios
survive). Researched and egress-verified 2026-11-04
(`research/hosting-options-2026-11-04.md`).

## What works / what doesn't on the free tier (verified whitelist)

| Source | Reachable? | Consequence |
|---|---|---|
| api.census.gov | ✓ | Demographics fallback source (v3.6) |
| api.censusreporter.org | ✗ blocked | Demographics primary — unreachable here, the fallback above serves instead |
| realtor.com | ✗ blocked | **HomeHarvest comp pulls cannot run here yet** — a pull fails with a clear in-app message ("can't be reached from this server"), stores nothing, and never dumps an error. Comps unlock when the RapidAPI source lands (p.rapidapi.com ✓ whitelisted; the merge seam is ready for it). |
| zillow.com / redfin.com | ✓ / ✗ | No comp frame uses them in the pinned HomeHarvest (Realtor-only) |
| overpass-api.de, nominatim | ✓ | Nearby places work |
| gis.hcpafl.org / gis.dupageco.org | not whitelist-checked | County parcel fill may or may not answer; it fails silent-honest (Not found) if blocked |

## First-time setup

1. **Clone the code** (Consoles tab → Bash):
   ```bash
   cd ~
   git clone https://github.com/sherifmoner/property-deal-analyzer.git
   ```
   The repo root IS the app (app.py, templates/, static/, analyzer.db).
2. **Virtualenv + dependencies:**
   ```bash
   mkvirtualenv --python=/usr/bin/python3.10 analyzer-venv   # use the newest python3 PA offers
   workon analyzer-venv
   cd ~/property-deal-analyzer
   pip install -r requirements.txt
   ```
3. **Web app** (Web tab → *Add a new web app* → **Manual configuration**,
   Python version matching the virtualenv). Then in the Web tab:
   - *Virtualenv:* path to `analyzer-venv`.
   - *WSGI configuration file:* open it and paste the contents of
     `wsgi_pythonanywhere.py` from this repo, replacing `YOUR_USERNAME`
     with the account username.
   - Click **Reload**. Open `https://<username>.pythonanywhere.com`.

## The database — read this before updating anything

`analyzer.db` sits in the project tree and **persists** on PA's disk —
it holds every saved property and scenario.

- The clone brings the shipped seed DB. From the first save onward, the
  PA copy is the LIVE database and diverges from the repo copy.
- **Code updates pull code only.** Before any update:
  ```bash
  cp ~/property-deal-analyzer/analyzer.db ~/analyzer-backup-$(date +%F).db
  cd ~/property-deal-analyzer && git pull
  # if the pull touched analyzer.db, restore the live one:
  cp ~/analyzer-backup-$(date +%F).db ~/property-deal-analyzer/analyzer.db
  ```
  Then Reload the web app from the Web tab.
- Never re-upload or overwrite `analyzer.db` from the repo after go-live.

## Recurring chore

- **Every 3 months:** Web tab → click **"Run until 3 months from today"**
  (extend) on the web app. PA emails a reminder first; data is untouched
  even if clicked late — the site just pauses until extended.

## Optional: Census API key

Demographics work without one where Census Reporter answers. On PA,
Reporter is blocked, so the app uses direct api.census.gov — keyless
queries may be refused by Census with a "Missing Key" page, in which
case a free key (api.census.gov key signup, Sherif's own) removes the
limit: set `CENSUS_API_KEY` in the WSGI file (see the template) or in
the app's settings store. Never commit a real key to the repo.

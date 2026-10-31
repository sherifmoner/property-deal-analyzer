#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then echo "First run: setting up..."; python3 -m venv .venv; fi
.venv/bin/pip install -q -r requirements.txt
( sleep 2; (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:5050) || open http://127.0.0.1:5050 ) &
echo "Property Deal Analyzer running at http://127.0.0.1:5050 — keep this window open; close it to stop."
.venv/bin/python app.py

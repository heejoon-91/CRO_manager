#!/bin/sh
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt; } || exit 1
exec .venv/bin/python app.py

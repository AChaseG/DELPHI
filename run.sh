#!/usr/bin/env bash
# Start Delphi (backend API + static frontend on one port).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

# --no-proxy-headers: the rate limiter works out who is calling from
# X-Forwarded-For and the socket peer (see backend/app/ratelimit.py). Uvicorn
# rewrites that peer from the same header unless told not to, and then neither
# layer can be reasoned about on its own.
exec .venv/bin/uvicorn backend.app.main:app --host "${HOST:-127.0.0.1}" \
     --port "${PORT:-8000}" --no-proxy-headers

#!/usr/bin/env bash
# Start (or restart) the dashboard in the background. Used by the devcontainer
# postStart hook; safe to run by hand any time: bash .devcontainer/start.sh
set -u
cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"

# Prefer a project venv if one exists; otherwise the system interpreter.
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

# Ensure dependencies are present even if the postCreate hook was skipped.
"$PY" -c "import uvicorn, fastapi, sqlalchemy, feedparser, httpx" 2>/dev/null \
  || "$PY" -m pip install -r requirements.txt

# Replace any previous instance, then launch detached.
# `python -m uvicorn` (not bare `uvicorn`) avoids PATH issues in
# non-interactive shells, where ~/.local/bin may be missing.
pkill -f "uvicorn backend.app.main:app" 2>/dev/null
sleep 1
nohup "$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" \
  > /tmp/news-dashboard.log 2>&1 &
disown 2>/dev/null || true

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/meta" > /dev/null 2>&1; then
    echo "✔ Global News Dashboard is up on port $PORT"
    exit 0
  fi
  sleep 1
done

echo "✘ Server did not come up. Last log lines (/tmp/news-dashboard.log):" >&2
tail -20 /tmp/news-dashboard.log >&2
exit 1

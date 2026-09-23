#!/usr/bin/env bash
# Starts Hackercon Tracker and opens it in your browser.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up a private Python environment (one-time, ~1 min)…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
PORT="${PORT:-8765}"
HOST="${HOST:-0.0.0.0}"
echo "Hackercon Tracker → http://localhost:$PORT   (Ctrl+C to stop)"
( sleep 2; xdg-open "http://localhost:$PORT" >/dev/null 2>&1 || true ) &
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"

#!/usr/bin/env bash
set -e
CHROME_PATH="${CHROME_PATH:-google-chrome}"
PORT="${PORT:-9222}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.chrome-fidelity-debug}"
URL="${URL:-https://digital.fidelity.com/}"
exec "$CHROME_PATH" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  "$URL"

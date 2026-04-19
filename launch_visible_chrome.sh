#!/usr/bin/env bash
# launch_visible_chrome.sh
# Launch a visible Chrome/Chromium with remote debugging for the Fidelity workflow.
#
# Environment variables (all optional):
#   CHROME_PATH     — path to Chrome binary     (auto-detected if unset)
#   PORT            — remote debugging port      (default: 9222)
#   PROFILE_DIR     — persistent profile dir     (default: ~/.fidelity-chrome-profile)
#   URL             — starting URL               (default: https://www.fidelity.com)
#
# Usage:
#   chmod +x launch_visible_chrome.sh
#   ./launch_visible_chrome.sh
#   PORT=9333 ./launch_visible_chrome.sh

set -euo pipefail

PORT="${PORT:-9222}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.fidelity-chrome-profile}"
URL="${URL:-https://www.fidelity.com}"

# Auto-detect Chrome binary.
if [[ -n "${CHROME_PATH:-}" ]]; then
  BINARY="$CHROME_PATH"
else
  for candidate in \
    "/usr/bin/google-chrome-stable" \
    "/usr/bin/google-chrome" \
    "/usr/bin/chromium-browser" \
    "/usr/bin/chromium" \
    "/snap/bin/chromium" \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
    if [[ -x "$candidate" ]]; then
      BINARY="$candidate"
      break
    fi
  done
fi

if [[ -z "${BINARY:-}" ]]; then
  echo "ERROR: Could not find Chrome or Chromium. Set CHROME_PATH and try again." >&2
  exit 1
fi

echo "Browser : $BINARY"
echo "Port    : $PORT"
echo "Profile : $PROFILE_DIR"
echo "URL     : $URL"
echo ""
echo "Launching... (attach OpenManus with: attach_visible_browser cdp_url=http://localhost:$PORT)"
echo ""

mkdir -p "$PROFILE_DIR"

exec "$BINARY" \
  --remote-debugging-port="$PORT" \
  --remote-allow-origins="*" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu \
  --no-sandbox \
  --disable-dev-shm-usage \
  "$URL"

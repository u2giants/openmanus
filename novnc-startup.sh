#!/bin/bash
# Wait for desktop environment to be fully ready
sleep 10

# Launch Chromium with remote debugging enabled
su -c 'DISPLAY=:1 chromium-browser \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu \
  --remote-debugging-port=9222 \
  --remote-debugging-address=0.0.0.0 \
  --user-data-dir=/config/chromium-profile \
  &' abc

# Wait until CDP endpoint is actually ready (up to 60s)
echo "Waiting for Chromium CDP to be ready..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:9222/json/version > /dev/null 2>&1; then
    echo "Chromium CDP ready after ${i}s"
    exit 0
  fi
  sleep 2
done
echo "WARNING: Chromium CDP not ready after 60s — browser automation may fail"

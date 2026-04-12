#!/bin/bash
# Wait for desktop to be ready
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

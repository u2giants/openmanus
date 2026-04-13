#!/bin/bash
# Wait for desktop environment to be fully ready
sleep 10

# Add Chromium to MATE autostart so it opens every time the desktop session starts
mkdir -p /config/.config/autostart
cat > /config/.config/autostart/chromium.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Chromium
Exec=chromium-browser --no-first-run --no-default-browser-check --disable-gpu --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir=/config/chromium-profile
Hidden=false
NoDisplay=false
X-MATE-Autostart-enabled=true
EOF

# Launch Chromium now (for the current session).
# Newer Chrome always binds CDP to 127.0.0.1; cdp_proxy.py exposes it on 9223/9224.
su -c 'DISPLAY=:1 chromium-browser \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --user-data-dir=/config/chromium-profile \
  &' abc

# Wait until Chromium CDP is up on loopback
echo "Waiting for Chromium CDP..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:9222/json/version > /dev/null 2>&1; then
    echo "Chromium CDP ready after $((i*2))s"
    break
  fi
  sleep 2
done

# Start CDP proxy (HTTP:9223 for JSON discovery, WS:9224 for WebSocket tunnel)
echo "Starting CDP proxy..."
python3 /custom-cont-init.d/cdp_proxy.py &
echo "CDP proxy running"

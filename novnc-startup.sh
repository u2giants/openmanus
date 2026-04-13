#!/bin/bash
# Wait for desktop environment to be fully ready
sleep 10

# Add Chromium to MATE autostart
mkdir -p /config/.config/autostart
cat > /config/.config/autostart/chromium.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Chromium
Exec=/usr/lib/chromium/chromium --no-first-run --no-default-browser-check --disable-gpu --no-sandbox --disable-dev-shm-usage --proxy-server=socks5://10.0.4.1:1080 --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir=/config/chromium-profile
Hidden=false
NoDisplay=false
X-MATE-Autostart-enabled=true
EOF

# Launch Chromium for the current session
su -c 'DISPLAY=:1 /usr/lib/chromium/chromium \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu \
  --no-sandbox \
  --disable-dev-shm-usage \
  --proxy-server=socks5://10.0.4.1:1080 \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --user-data-dir=/config/chromium-profile \
  &' abc

# Wait until Chromium CDP is up
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

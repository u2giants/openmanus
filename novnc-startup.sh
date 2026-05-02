#!/bin/bash
# Write Chromium autostart for the MATE session.
# Running inside the MATE session gives the correct DISPLAY and D-Bus
# environment without needing to su or set DISPLAY manually.
# The Exec is a watchdog loop: it clears stale singleton locks and restarts
# Chromium automatically whenever it exits (crash, OOM, user close, etc.).
mkdir -p /config/.config/autostart
cat > /config/.config/autostart/chromium.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Chromium
Exec=/bin/bash -c "while true; do find /config/chromium-profile -maxdepth 1 -iname 'singleton*' -delete 2>/dev/null; /usr/lib/chromium/chromium --no-first-run --no-default-browser-check --disable-gpu --no-sandbox --disable-dev-shm-usage --disable-blink-features=AutomationControlled --remote-debugging-port=9222 --remote-allow-origins='*' --user-data-dir=/config/chromium-profile https://www.fidelity.com; sleep 3; done"
Hidden=false
NoDisplay=false
X-MATE-Autostart-enabled=true
EOF

# Wait until Chromium CDP is up (MATE starts Chromium after X desktop is ready)
echo "Waiting for Chromium CDP..."
for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
        echo "Chromium CDP ready after $((i*2))s"
        break
    fi
    sleep 2
done

# Start CDP proxy (HTTP :9223 for JSON discovery, WS :9224 for WebSocket tunnel)
echo "Starting CDP proxy..."
python3 /custom-cont-init.d/cdp_proxy.py &
echo "CDP proxy running"

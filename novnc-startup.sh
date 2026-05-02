#!/bin/bash
CHROME_BIN=/usr/lib/chromium/chromium
CHROME_PROFILE=/config/chromium-profile
CHROME_LOG=/config/chromium.log

_launch_chrome() {
    # Clear stale singleton locks before each launch attempt
    find "$CHROME_PROFILE" -maxdepth 1 -iname "singleton*" -delete 2>/dev/null
    su -c "DISPLAY=:1 $CHROME_BIN \
        --no-first-run \
        --no-default-browser-check \
        --disable-gpu \
        --no-sandbox \
        --disable-dev-shm-usage \
        --remote-debugging-port=9222 \
        --remote-allow-origins='*' \
        --user-data-dir=$CHROME_PROFILE \
        https://www.fidelity.com" abc >>"$CHROME_LOG" 2>&1
}

# Wait for the X display to be ready before touching it
echo "Waiting for X display :1..."
for i in $(seq 1 30); do
    [ -S /tmp/.X11-unix/X1 ] && break
    sleep 1
done

# Chrome watchdog: runs Chrome in the foreground; restarts it automatically
# whenever it exits (crash, OOM kill, user close, etc.)
(while true; do
    echo "[chromium-watchdog] Launching Chromium..."
    _launch_chrome
    echo "[chromium-watchdog] Chromium exited. Restarting in 3s..."
    sleep 3
done) &

# Wait until Chromium CDP is up (max 120 s)
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

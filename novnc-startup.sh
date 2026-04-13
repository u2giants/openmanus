#!/bin/bash
# Wait for desktop environment to be fully ready
sleep 10

# Launch Chromium. Newer Chrome always binds CDP to 127.0.0.1 regardless of
# --remote-debugging-address, so we proxy it out on port 9223 below.
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

# Python proxy: listens on 0.0.0.0:9223, rewrites Host header to localhost:9222
# so Chrome's host-check security doesn't reject cross-container connections.
echo "Starting CDP proxy on 0.0.0.0:9223..."
python3 - << 'PYEOF' &
import socket, threading, re

def handle(client):
    server = socket.socket()
    server.connect(('127.0.0.1', 9222))
    def fwd(src, dst, rewrite_host=False):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                if rewrite_host and b'Host:' in data:
                    data = re.sub(rb'Host: [^\r\n]+', b'Host: localhost:9222', data)
                dst.sendall(data)
        except:
            pass
        finally:
            try: src.close()
            except: pass
            try: dst.close()
            except: pass
    threading.Thread(target=fwd, args=(client, server, True), daemon=True).start()
    fwd(server, client)

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', 9223))
s.listen(50)
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
PYEOF
echo "CDP proxy running on port 9223"

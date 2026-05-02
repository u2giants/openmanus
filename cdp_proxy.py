"""
CDP proxy for noVNC container.
- HTTP on :9223  — proxies Chrome's JSON endpoints, rewrites localhost:9222 → novnc:9224
- WS   on :9224  — tunnels WebSocket CDP connections to Chrome on localhost:9222

Chrome binds the CDP debugger to ::1 (IPv6 loopback) regardless of
--remote-debugging-address, so this proxy makes it reachable from other containers.
"""

import asyncio
import re
import urllib.request
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


async def pipe(r, w):
    try:
        while True:
            d = await r.read(65536)
            if not d:
                break
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def ws_handle(cr, cw):
    # Buffer until we have the full HTTP upgrade request headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await cr.read(4096)
        if not chunk:
            return
        buf += chunk
    # Rewrite Host so Chrome accepts the connection
    req = re.sub(rb"Host: [^\r\n]+", b"Host: localhost:9222", buf)
    sr, sw = await asyncio.open_connection("::1", 9222)
    sw.write(req)
    await sw.drain()
    # Forward Chrome's 101 Switching Protocols response
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = await sr.read(4096)
        if not chunk:
            break
        resp += chunk
    cw.write(resp)
    await cw.drain()
    # Transparent bidirectional tunnel for the WebSocket frames
    await asyncio.gather(pipe(cr, sw), pipe(sr, cw))


def run_ws_server():
    async def main():
        srv = await asyncio.start_server(ws_handle, "0.0.0.0", 9224)
        async with srv:
            await srv.serve_forever()

    asyncio.run(main())


threading.Thread(target=run_ws_server, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            req = urllib.request.Request(
                "http://[::1]:9222" + self.path,
                headers={"Host": "localhost:9222"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().replace(b"localhost:9222", b"novnc:9224")
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("content-type", "cache-control"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            self.send_error(502, str(e))


HTTPServer(("0.0.0.0", 9223), Handler).serve_forever()

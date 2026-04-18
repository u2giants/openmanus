# Troubleshooting Guide

Operational runbook for the OpenManus stack deployed via Coolify.

---

## Bad Gateway on /admin/tools

**Symptom**: `https://manus.designflow.app/admin/tools` returns 502 Bad Gateway.

**Root cause**: Stale Traefik dynamic proxy config intercepting `/admin/*` and routing to a dead upstream.

**Fix**:
1. SSH to the server and check for stale dynamic configs:
   ```bash
   ls /data/coolify/proxy/dynamic/
   cat /data/coolify/proxy/dynamic/tool-manager.yaml
   ```
2. If the file routes `/admin/*` or `/api/tools` to an old/dead IP, delete it:
   ```bash
   rm /data/coolify/proxy/dynamic/tool-manager.yaml
   ```
3. Traefik picks up the change automatically within seconds. Verify:
   ```bash
   curl -sS -o /dev/null -w "%{http_code}" https://manus.designflow.app/admin/tools
   ```
4. Expected: `200`

**Prevention**: The CI smoke check job (in [`.github/workflows/build-deploy.yml`](.github/workflows/build-deploy.yml)) now curls `/admin/tools` and `/api/tools` after every deployment. If a stale proxy config reappears, the check will fail with a visible error in GitHub Actions.

---

## Stale Deployment — Coolify Not Pulling New Image

**Symptom**: Code changes pushed to `main` and built successfully, but the running container still serves old code.

**Root cause**: By default, `docker-compose up -d` reuses locally cached images when the tag (`:main`) already exists. Coolify's redeploy was restarting the container with the old image.

**Fix**: Verify `pull_policy: always` is set on all services in [`docker-compose.yaml`](docker-compose.yaml):
```yaml
services:
  openmanus-backend:
    pull_policy: always
  open-webui:
    pull_policy: always
  novnc:
    pull_policy: always
```

**Verification**: After a deploy, check the image digest on the server:
```bash
docker inspect --format='{{.Image}}' openmanus-backend
```
Compare with the digest pushed to GHCR in the GitHub Actions log.

---

## Browser Automation Fails / CDP Connection Refused

**Symptom**: Agent reports `RetryError`, `ConnectionRefusedError`, or browser tool fails silently.

**Checks**:

1. **Verify mount types** — `novnc-startup.sh` and `cdp_proxy.py` must be mounted as **files**, not directories. If Docker created them as directories (common when the file doesn't exist at create time), the scripts won't execute:
   ```bash
   docker exec novnc ls -la /custom-cont-init.d/99-start-chromium.sh
   docker exec novnc ls -la /custom-cont-init.d/cdp_proxy.py
   ```
   If these are directories instead of files, see [Volume Mounts Created as Directories](#volume-mounts-created-as-directories).

2. **Check Chromium is running** inside the novnc container:
   ```bash
   docker exec novnc ps aux | grep chromium
   ```

3. **Check CDP is listening** on localhost inside novnc:
   ```bash
   docker exec novnc curl -s http://127.0.0.1:9222/json/version
   ```
   Expected: JSON with `Browser` and `webSocketDebuggerUrl` fields.

4. **Check CDP proxy is running**:
   ```bash
   docker exec novnc ps aux | grep cdp_proxy
   ```

5. **Check CDP proxy from the backend container**:
   ```bash
   docker exec openmanus-backend curl -s http://novnc:9223/json/version
   ```
   Expected: Same JSON as step 3, but with URLs rewritten to `novnc:9224`.

6. **Check the `BROWSER_CDP_URL` env var** in the backend:
   ```bash
   docker exec openmanus-backend env | grep BROWSER_CDP_URL
   ```
   Expected: `BROWSER_CDP_URL=http://novnc:9223`

---

## Volume Mounts Created as Directories

**Symptom**: A bind mount like `./novnc-startup.sh:/custom-cont-init.d/99-start-chromium.sh:ro` results in a **directory** inside the container instead of a file. Scripts fail with `permission denied` or `is a directory`.

**Root cause**: Docker caches the mount type (file vs. directory) at container creation time. If the source file didn't exist when the container was first created, Docker creates a directory at the mount point. Subsequent `docker compose up` calls reuse the cached directory mount even after the source file exists.

**Fix**:
1. Stop and **remove** the container (not just stop — remove):
   ```bash
   docker compose down
   # Or specifically:
   docker rm -f novnc
   ```
2. Verify the source files exist on the host at the paths specified in `docker-compose.yaml`.
3. Recreate:
   ```bash
   docker compose up -d
   ```
4. Verify the mount is a file:
   ```bash
   docker exec novnc file /custom-cont-init.d/99-start-chromium.sh
   ```
   Expected: `ASCII text` or similar, NOT `directory`.

---

## CRLF Line Endings Break Scripts

**Symptom**: Shell scripts fail with `/bin/bash^M: bad interpreter` or Python scripts fail with `SyntaxError` on the server but work locally on Windows.

**Root cause**: Git on Windows may check out files with CRLF (`\r\n`) line endings. When these are copied into a Linux container, the `\r` characters cause interpreter failures.

**Fix**: Run `sed` or `dos2unix` on the server to strip carriage returns:
```bash
sed -i 's/\r$//' /path/to/script.sh
```

**Prevention**: Ensure `.gitattributes` contains:
```
*.sh text eol=lf
*.py text eol=lf
```

---

## LSIO custom-cont-init.d Runs Before X Server

**Symptom**: Scripts in `/custom-cont-init.d/` that try to launch GUI apps (like Chromium) fail with `Missing X server or $DISPLAY`.

**Root cause**: LinuxServer.io (LSIO) containers run `custom-cont-init.d` scripts **before** the X server and desktop environment start. GUI apps cannot launch without a display.

**Fix**: Don't launch GUI apps from `custom-cont-init.d`. Instead, use the desktop environment's autostart mechanism. For MATE (used by `webtop:ubuntu-mate`), place a `.desktop` file in `/config/.config/autostart/`:

```ini
[Desktop Entry]
Type=Application
Name=Chromium
Exec=/usr/lib/chromium/chromium --remote-debugging-port=9222 ...
X-MATE-Autostart-enabled=true
```

The `custom-cont-init.d` script can **create** the `.desktop` file, but should not try to launch Chromium directly. See [`novnc-startup.sh`](novnc-startup.sh) for the working implementation.

---

## Container Health Checks

| Service | Health Check | Notes |
|---------|-------------|-------|
| `open-webui` | Built-in Docker `HEALTHCHECK` | Checks `/api/health` endpoint |
| `openmanus-backend` | None | Relies on `restart: unless-stopped` policy |
| `novnc` | None | Relies on `restart: unless-stopped` policy |

The backend exposes a `/health` endpoint (defined in [`server.py`](server.py)) but does not have a Docker `HEALTHCHECK` instruction. To check manually:

```bash
curl -s http://localhost:8001/health
```

---

## Disk Space Warnings

**Symptom**: Server reports high disk usage, containers fail to start or write files.

**Check**:
```bash
df -h
```

The server has been observed at ~78% disk usage. Key consumers:
- Docker images (old tags not pruned)
- Docker volumes (noVNC data, Open WebUI data)
- Build cache

**Cleanup**:
```bash
# Remove unused images
docker image prune -a --filter "until=168h"
# Remove unused volumes (careful — check what's unused first)
docker volume prune
# Check Docker disk usage
docker system df
```

---

## How to Verify CDP Is Working

From inside the novnc container, check that Chromium's CDP debugger is responding:

```bash
docker exec novnc curl -s http://127.0.0.1:9222/json/version
```

Expected output (example):
```json
{
  "Browser": "Chromium/130.0.6723.69",
  "Protocol-Version": "1.3",
  "User-Agent": "...",
  "V8-Version": "13.0.245.14",
  "WebKit-Version": "537.36",
  "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/..."
}
```

If this returns nothing or `Connection refused`, Chromium is not running or CDP is not enabled. Check:
1. Chromium process exists: `docker exec novnc ps aux | grep chromium`
2. MATE autostart `.desktop` file exists: `docker exec novnc cat /config/.config/autostart/chromium.desktop`
3. X server is running: `docker exec novnc ps aux | grep X`

---

## How to Verify CDP Proxy Is Working

From the backend container, check that the CDP proxy is forwarding requests correctly:

```bash
docker exec openmanus-backend curl -s http://novnc:9223/json/version
```

Expected: Same JSON as the direct CDP check above, but with `webSocketDebuggerUrl` rewritten to use `novnc:9224` instead of `127.0.0.1:9222`.

If this fails:
1. Check the CDP proxy process: `docker exec novnc ps aux | grep cdp_proxy`
2. Check the proxy is listening: `docker exec novnc ss -tlnp | grep -E '9223|9224'`
3. Check that direct CDP works first (see [How to Verify CDP Is Working](#how-to-verify-cdp-is-working))
4. Check that `novnc-startup.sh` started the proxy (look for `Starting CDP proxy...` in container logs)

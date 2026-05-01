# Troubleshooting Guide

Operational runbook for the OpenManus stack deployed via Coolify.

---

## /admin/tools Returns 404 or 502

**Symptom**: `https://manus.designflow.app/admin/tools` returns 404 Not Found or 502 Bad Gateway.

### Cause A: Stale Traefik dynamic config (502)

A stale file in `/data/coolify/proxy/dynamic/` is intercepting the route and sending it to a dead upstream.

**Fix**:
1. SSH to the server and check for stale dynamic configs:
   ```bash
   ls /data/coolify/proxy/dynamic/
   cat /data/coolify/proxy/dynamic/tool-manager.yaml  # if it exists
   ```
2. If the file routes `/admin/*` or `/api/tools` to an old/dead IP, delete it:
   ```bash
   rm /data/coolify/proxy/dynamic/tool-manager.yaml
   ```
3. Traefik picks up the change automatically within seconds. Verify:
   ```bash
   curl -sS -o /dev/null -w "%{http_code}" https://manus.designflow.app/admin/tools
   ```
   Expected: `200`

### Cause B: Wrong Traefik rule in docker-compose.yaml (404)

If the Traefik label uses `PathPrefix('/admin/')` instead of `Path('/admin/tools')`, the openmanus-backend router takes priority but the backend only handles `/admin/tools` — all other `/admin/*` paths return 404 from the backend, AND OpenWebUI's own admin panel breaks silently (see below).

**The correct rule** in `docker-compose.yaml`:
```yaml
- "traefik.http.routers.openmanus-admin.rule=Host(`manus.designflow.app`) && (Path(`/admin/tools`) || PathPrefix(`/api/tools`) || PathPrefix(`/api/owui`))"
```

### Cause C: Coolify deployed from stale DB compose (404 or route missing)

If you changed `docker-compose.yaml` but triggered Coolify manually (without a CI run), Coolify deployed from its stale DB copy and the route change wasn't picked up.

**Fix**: Push to `main` to trigger a full CI run, which syncs the compose to Coolify's DB before deploying.

**Prevention**: The CI smoke check job now curls `/admin/tools` after every deployment. If a stale proxy config reappears or the route is missing, the check will fail with a visible error in GitHub Actions.

---

## OpenWebUI Admin Panel Save Buttons Don't Work

**Symptom**: In the OpenWebUI admin panel (`/admin/settings`, `/admin/users`, etc.), clicking "Save" shows a spinner for a split-second and then nothing happens. The dialog stays open.

**Root cause**: A Traefik rule using `PathPrefix('/admin/')` to route to openmanus-backend is intercepting OpenWebUI's admin API calls and returning 404. The page loads fine (OpenWebUI's SPA is served by the catch-all rule), but all admin API POST calls go to the backend.

**Fix**: Ensure the openmanus-backend Traefik rule uses `Path('/admin/tools')` (exact match), not `PathPrefix('/admin/')`. See the correct rule above.

---

## open-webui Goes Down on Every Deploy

**Symptom**: Every push to `main` causes `https://manus.designflow.app` to be unavailable for 90+ seconds.

**Root cause**: `pull_policy: always` on `open-webui` forces a full image pull + container restart on every deploy, even though the open-webui image doesn't change on our pushes.

**Fix**: Set `pull_policy: if_not_present` on `open-webui` and `novnc` in `docker-compose.yaml`:
```yaml
open-webui:
  pull_policy: if_not_present
novnc:
  pull_policy: if_not_present
```

Only `openmanus-backend` should have `pull_policy: always`.

**To upgrade open-webui or novnc to a newer version**: Change the image tag in docker-compose.yaml (or remove the existing image on the host and trigger a deploy).

---

## Do Not Add Docker Healthchecks

**Symptom**: After adding a `HEALTHCHECK` to `openmanus-backend` and deploying, `open-webui` never starts.

**Root cause**: Coolify silently converts `depends_on: openmanus-backend` to `depends_on: openmanus-backend: condition: service_healthy` whenever the dependency has a Docker healthcheck. If the healthcheck fails (including `curl` not being in the image), `open-webui` waits indefinitely and never starts.

**Do not add a Docker `HEALTHCHECK` to any service that other services depend on.** The `/health` endpoint in `server.py` is for external uptime monitoring only. Use UptimeRobot, Coolify's monitoring UI, or the CI smoke check instead.

---

## OpenWebUI Tool Sync Returns 405 or Synced 0 Tools

**Symptom**: Clicking "↻ Sync All" in the Tool Manager shows "Synced 0 tool(s) to OpenWebUI" or a tool name followed by "HTTP 405: Method Not Allowed".

### Cause A: Not logged in (no session cookie)

The sync uses your browser's OpenWebUI session JWT. If you're not logged into OpenWebUI, there's no token.

**Fix**: Log into `https://manus.designflow.app`, then try syncing again from the same browser session.

### Cause B: Wrong OpenWebUI API paths (missing /id/ prefix)

OpenWebUI 0.8.x requires `/id/` in per-resource API paths. Without it, GET requests hit the SPA and return 200 HTML (making existence checks always true), and POST requests return 405.

**This should already be fixed in the current `server.py`** — all three helper functions (`_owui_sync_one`, `_owui_delete_one`, `_owui_install_fn`) use the `/id/` prefix. If you see 405 errors again after a code change, verify that paths in these helpers still include `/id/`.

Correct paths for OpenWebUI 0.8.x:
- Existence check: `GET /api/v1/tools/id/{id}`
- Update: `POST /api/v1/tools/id/{id}/update`
- Delete: `DELETE /api/v1/tools/id/{id}/delete`
- Create (no `/id/`): `POST /api/v1/tools/create`

### Cause C: OWUI URL misconfigured

Check Settings → OWUI URL in the Tool Manager. Default is `http://open-webui:8080` (internal Docker network name). This must be reachable from the backend container.

---

## Coolify DB Drift — Compose Changes Not Deployed

**Symptom**: You changed `docker-compose.yaml`, pushed to `main`, and the CI deploy succeeded — but the change (a new env var, Traefik rule, volume mount, etc.) isn't live.

**Root cause**: Coolify deploys from its internal database copy of the compose file. If the CI workflow's "Sync compose to Coolify DB" step failed silently, Coolify deployed from the old copy.

**Check**: Look at the CI run in GitHub Actions. The "Sync compose to Coolify DB" step should show `HTTP 200` or `HTTP 2xx`. If it shows an error status, the sync failed.

**Manual fix**: You can manually PATCH the compose into Coolify's DB and trigger a deploy:
```bash
COMPOSE=$(base64 -w 0 docker-compose.yaml)
curl -X PATCH \
  -H "Authorization: Bearer $COOLIFY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"docker_compose_raw\": \"$COMPOSE\"}" \
  "http://178.156.180.212:8000/api/v1/services/e10kwzww46ljhrgz1qj08j6a"
```

---

## Stale Entries in Coolify Dashboard

**Symptom**: Multiple entries for the openmanus stack appear in the Coolify UI, most showing red (stopped/failed). Only one should exist.

**Root cause**: Coolify creates new service entries when the stack is re-imported or when an "Application" type was converted to a "Service" type, leaving orphan entries.

**Fix**: Delete stale entries via the Coolify API (get their UUIDs from the Coolify UI URL and DELETE `/api/v1/services/{uuid}`). Keep only the entry with UUID `e10kwzww46ljhrgz1qj08j6a`.

**Only the Service type** (`e10kwzww46ljhrgz1qj08j6a`) is correct. Do not create Application-type entries for this stack.

---

## Stale Deployment — Coolify Not Pulling New Image

**Symptom**: Code changes pushed to `main` and built successfully, but the running container still serves old code.

**Root cause**: `pull_policy: always` is missing on `openmanus-backend`. Without it, `docker compose up -d` reuses the locally cached image.

**Fix**: Verify `pull_policy: always` is set on `openmanus-backend` in [`docker-compose.yaml`](docker-compose.yaml):
```yaml
openmanus-backend:
  pull_policy: always
```

**Verification**: After a deploy, check the image digest on the server:
```bash
docker inspect --format='{{.Image}}' openmanus-backend
```
Compare with the digest pushed to GHCR in the GitHub Actions log.

---

## Browser Automation Fails / CDP Connection Refused

**Symptom**: Agent reports `RetryError`, `ConnectionRefusedError`, or browser tool fails silently. Or the CDP proxy on port 9223 returns HTTP 502.

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

### Cause: Stale Chromium singleton lock from previous container instance

**Symptom**: Port 9223 is open (proxy is running) but returns 502. Chromium is not in `ps aux`. The `novnc-startup.sh` startup log shows the CDP wait timed out.

**Root cause**: Chromium writes `SingletonLock`, `SingletonCookie`, and `SingletonSocket` files to the profile directory when it starts. These files record the process ID and hostname. When the noVNC container is recreated (new hostname), Chromium finds a lock from a different "computer" and refuses to start (exits with code 21). The profile directory is a named Docker volume (`novnc-data`) that persists across container restarts and recreations, so the stale lock survives.

**Automatic fix**: `novnc-startup.sh` now clears these files at every container init:
```bash
find /config/chromium-profile -maxdepth 1 -iname "singleton*" -delete
```
This runs before Chromium's MATE autostart fires, so stale locks are always cleared on startup.

**Manual fix** (if you need to clear without a container restart):
```bash
docker exec novnc find /config/chromium-profile -maxdepth 1 -iname "singleton*" -delete
```
Chromium will start on its next attempt (MATE autostart or manual launch).

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

## Coolify Healthcheck Warning

**Symptom**: Coolify UI shows a warning: "No health check configured. The resource may be functioning normally..."

**This is expected and safe to ignore.** No Docker healthchecks are configured on any service — intentionally, because Coolify silently converts `depends_on` to `condition: service_healthy` when a healthcheck is present, which prevents dependent services from starting. See [Do Not Add Docker Healthchecks](#do-not-add-docker-healthchecks) above.

---

## Container Health Checks (Manual)

The backend exposes a `/health` endpoint for external monitoring:
```bash
curl -s http://localhost:8001/health
```

No Docker `HEALTHCHECK` instruction exists on any service — intentional. See above.

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

From inside the novnc container:
```bash
docker exec novnc curl -s http://127.0.0.1:9222/json/version
```

Expected output:
```json
{
  "Browser": "Chromium/130.0.6723.69",
  "Protocol-Version": "1.3",
  "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/..."
}
```

If this returns nothing or `Connection refused`:
1. Chromium process: `docker exec novnc ps aux | grep chromium`
2. MATE autostart file: `docker exec novnc cat /config/.config/autostart/chromium.desktop`
3. X server running: `docker exec novnc ps aux | grep X`

---

## How to Verify CDP Proxy Is Working

From the backend container:
```bash
docker exec openmanus-backend curl -s http://novnc:9223/json/version
```

Expected: Same JSON as the direct CDP check above, but with `webSocketDebuggerUrl` rewritten to use `novnc:9224`.

If this fails:
1. Check the proxy process: `docker exec novnc ps aux | grep cdp_proxy`
2. Check ports listening: `docker exec novnc ss -tlnp | grep -E '9223|9224'`
3. Check direct CDP works first (see above)
4. Look for `Starting CDP proxy...` in container logs: `docker logs novnc | grep -i cdp`

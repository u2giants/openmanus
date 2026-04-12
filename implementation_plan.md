# Implementation Plan: Enable Browser Automation with Shared noVNC Visibility

## Problem

When a user asks OpenManus to browse the web (e.g., "navigate to fidelity.com and download statements"), the agent crashes with:

```
RetryError[<Future at 0x7351fe504950 state=finished raised NotFoundError>]
```

### Root Cause Analysis

The upstream OpenManus **already has browser automation built in**:
- [`app/tool/browser_use_tool.py`](https://github.com/FoundationAgents/OpenManus/blob/main/app/tool/browser_use_tool.py) — `BrowserUseTool` using `browser-use` library + Playwright
- [`app/agent/manus.py`](https://github.com/FoundationAgents/OpenManus/blob/main/app/agent/manus.py) — `Manus` agent includes `BrowserUseTool()` in its default tools
- [`requirements.txt`](https://github.com/FoundationAgents/OpenManus/blob/main/requirements.txt) — includes `playwright~=1.51.0` and `browser-use~=0.1.40`

**However, it fails because:**

1. **No Chromium binary installed**: The [`Dockerfile`](Dockerfile) never runs `playwright install chromium`. Playwright is installed as a Python package but has no browser binary to launch. This causes the `NotFoundError`.

2. **No display server**: `BrowserUseTool._ensure_browser_initialized()` defaults to `headless=False`, which tries to open a visible browser window. In a Docker container with no X11/display, this would fail even if Chromium were installed.

3. **No `[browser]` config section**: Our [`config.toml`](config.toml) only has `[llm]` and `[daytona]` sections. Without a `[browser]` section, the tool uses defaults (`headless=False`, no CDP/WSS URL).

4. **noVNC container is isolated**: The [`novnc` service](docker-compose.yaml:48) runs a full Ubuntu MATE desktop (`lscr.io/linuxserver/webtop:ubuntu-mate`) but has no connection to the OpenManus backend. The agent can't see or control anything in it.

### User's Requirements

The user wants:
1. **Agent can browse the web** — navigate to sites, click elements, fill forms, download files
2. **Shared visibility** — both the user and agent can see the same browser via noVNC
3. **Human-in-the-loop** — user can log in manually, then hand control back to the agent
4. **Practical use case** — download 10 years of Fidelity statements

## Solution

### Architecture: Headless Playwright in OpenManus + Separate noVNC for Observation

After evaluating multiple approaches, the recommended architecture is:

```
┌─────────────────────────────────────────────────────────┐
│  User's Browser                                          │
│                                                          │
│  ┌──────────────────┐    ┌─────────────────────────┐    │
│  │ manus.designflow │    │  vnc.designflow.app     │    │
│  │ .app (Open WebUI)│    │  (noVNC — shared view)  │    │
│  │                  │    │                          │    │
│  │  Chat with agent │    │  See what agent sees     │    │
│  │  Type commands   │    │  Take over mouse/kbd    │    │
│  └────────┬─────────┘    └──────────▲──────────────┘    │
│           │                         │                    │
└───────────┼─────────────────────────┼────────────────────┘
            │                         │
            ▼                         │ VNC protocol
   ┌────────────────┐       ┌────────┴───────────┐
   │ openmanus-     │       │  novnc container   │
   │ backend        │       │  (webtop:ubuntu-   │
   │                │  CDP  │   mate)             │
   │ server.py ─────┼──────►│                     │
   │ Manus agent    │       │  Chromium browser   │
   │ BrowserUseTool │       │  running inside     │
   │                │       │  with --remote-     │
   │                │       │  debugging-port     │
   └────────────────┘       └─────────────────────┘
```

**Key insight**: Instead of running Playwright's own Chromium inside the openmanus-backend container, we run Chromium **inside the noVNC/webtop container** with Chrome DevTools Protocol (CDP) enabled. The OpenManus `BrowserUseTool` connects to it via `cdp_url`. This gives us:

- ✅ **Shared visibility**: User sees the browser in noVNC, agent controls it via CDP
- ✅ **Human-in-the-loop**: User can take over mouse/keyboard in noVNC at any time
- ✅ **No display server needed in backend**: Backend connects remotely via CDP
- ✅ **Downloads visible**: Files download to the noVNC container's filesystem, visible to user
- ✅ **Minimal changes**: Uses existing upstream `BrowserUseTool` + `[browser]` config

### Alternative Considered: Headless Playwright in Backend Only

Run Playwright headless inside the openmanus-backend container. Simpler but:
- ❌ User can't see what the agent is doing in real-time
- ❌ No human-in-the-loop (can't log in manually)
- ❌ No shared browser session
- This would be a fallback if CDP approach proves too complex

### Alternative Considered: Playwright MCP Server

Run a separate Playwright MCP server container. Rejected because:
- ❌ OpenManus already has `BrowserUseTool` built in — adding MCP is redundant
- ❌ More containers, more complexity
- ❌ MCP stdio transport doesn't work across containers (would need SSE)

## Step-by-Step Tasks

### Task 1: Install Chromium in the noVNC/Webtop Container

**Owner**: DevOps / Builder  
**Files**: [`docker-compose.yaml`](docker-compose.yaml)

The `lscr.io/linuxserver/webtop:ubuntu-mate` image already includes a desktop environment. We need to:

1. Add a custom startup script that installs Chromium and launches it with CDP enabled
2. Create a `novnc-startup.sh` script that:
   - Installs `chromium-browser` (if not already present)
   - Launches Chromium with `--remote-debugging-port=9222 --remote-debugging-address=0.0.0.0`
   - Keeps the desktop session alive
3. Mount this script into the noVNC container via docker-compose volume
4. Expose port 9222 internally (not publicly — only to the Docker network)

**Config in `docker-compose.yaml`**:
```yaml
novnc:
  image: lscr.io/linuxserver/webtop:ubuntu-mate
  restart: unless-stopped
  environment:
    - PUID=1000
    - PGID=1000
    - TZ=America/New_York
    - CUSTOM_USER=abc
    - SUBFOLDER=/
    - DOCKER_MODS=linuxserver/mods:universal-package-install
    - INSTALL_PACKAGES=chromium-browser
  volumes:
    - novnc-data:/config
    - ./novnc-startup.sh:/custom-cont-init.d/99-start-chromium.sh:ro
  shm_size: '1gb'
  # Port 9222 exposed only on Docker network, not publicly
  expose:
    - "9222"
  labels:
    # ... existing traefik labels ...
```

### Task 2: Create noVNC Chromium Startup Script

**Owner**: Builder  
**Files**: NEW `novnc-startup.sh`

Create a script that auto-launches Chromium with CDP on the webtop desktop:

```bash
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
```

### Task 3: Update `config.toml` with Browser Configuration

**Owner**: Builder  
**Files**: [`config.toml`](config.toml)

Add a `[browser]` section that tells `BrowserUseTool` to connect via CDP to the noVNC container's Chromium:

```toml
[browser]
headless = false
disable_security = true
cdp_url = "http://novnc:9222"
```

The `cdp_url` uses the Docker Compose service name `novnc` which resolves within the Docker network. The `headless = false` is correct here because the browser is running on the noVNC desktop (which has a display), not in the backend container.

### Task 4: Install Playwright in the Backend Container (Fallback/Library Only)

**Owner**: Builder  
**Files**: [`Dockerfile`](Dockerfile)

Even though we're connecting to a remote browser via CDP, the `browser-use` Python library still needs Playwright installed. We need to ensure the Playwright Python bindings work. We do NOT need to install Chromium binaries in the backend container since we're using CDP.

Update the Dockerfile to:
1. Install system dependencies needed by Playwright Python bindings
2. Run `playwright install-deps` (installs OS-level dependencies only, not browser binaries)

```dockerfile
# After pip install
RUN pip install --no-cache-dir playwright && playwright install-deps
```

Note: `playwright install-deps` installs system libraries (libglib, libnss, etc.) that the Playwright Python package needs even when connecting to a remote browser. `playwright install chromium` is NOT needed since we use CDP.

### Task 5: Update `entrypoint.sh` for Browser Config Substitution

**Owner**: Builder  
**Files**: [`entrypoint.sh`](entrypoint.sh)

The `cdp_url` in config.toml should use the Docker service name, which is static. No env var substitution needed for this. However, we should add a `BROWSER_CDP_URL` env var for flexibility:

```bash
#!/bin/sh
sed -i "s|__OPENAI_API_KEY__|${OPENAI_API_KEY:-placeholder}|g" /app/config/config.toml
sed -i "s|__DAYTONA_API_KEY__|${DAYTONA_API_KEY:-not-configured}|g" /app/config/config.toml
sed -i "s|__BROWSER_CDP_URL__|${BROWSER_CDP_URL:-http://novnc:9222}|g" /app/config/config.toml
exec python server.py
```

And update `config.toml`:
```toml
[browser]
headless = false
disable_security = true
cdp_url = "__BROWSER_CDP_URL__"
```

### Task 6: Add `openmanus-backend` Dependency on `novnc`

**Owner**: Builder  
**Files**: [`docker-compose.yaml`](docker-compose.yaml)

Ensure the backend waits for the noVNC container to be ready:

```yaml
openmanus-backend:
  depends_on:
    - novnc
```

### Task 7: Verify noVNC Container is Running and Accessible

**Owner**: DevOps  
**Action**: After deployment, verify:
1. `vnc.designflow.app` loads the noVNC desktop
2. Chromium is running inside the desktop
3. CDP is accessible from the backend container: `curl http://novnc:9222/json/version`

### Task 8: Test End-to-End Browser Automation

**Owner**: DevOps / Manual  
**Action**:
1. Open `vnc.designflow.app` in a browser tab — you should see the Ubuntu MATE desktop with Chromium
2. Open `manus.designflow.app` in another tab
3. Ask the agent: "Navigate to example.com"
4. Watch the noVNC tab — Chromium should navigate to example.com
5. Verify the agent reports success in the chat

### Task 9: Test Human-in-the-Loop Flow

**Owner**: Manual  
**Action**:
1. Ask the agent: "Navigate to fidelity.com"
2. In the noVNC tab, manually log in to Fidelity
3. Tell the agent: "I've logged in. Now navigate to the statements page and download the last 12 months of statements"
4. Watch the agent work in noVNC while you monitor

## Validation Approach

| Check | Method | Expected Result |
|-------|--------|-----------------|
| Chromium running in noVNC | Visit `vnc.designflow.app` | See Chromium on desktop |
| CDP accessible | `docker exec openmanus-backend curl http://novnc:9222/json/version` | JSON response with Chrome version |
| Browser tool works | Ask agent "go to example.com" | Agent navigates, noVNC shows example.com |
| Screenshots work | Ask agent "take a screenshot of the current page" | Agent returns screenshot in chat |
| Human takeover | Click in noVNC while agent is idle | Mouse/keyboard work in noVNC |
| Downloads work | Ask agent to download a file | File appears in noVNC container |

## Rollback Plan

1. **If CDP connection fails**: Remove `[browser]` section from `config.toml`, revert to no browser config. The agent will still work for non-browser tasks.
2. **If noVNC container breaks**: Revert `docker-compose.yaml` to previous version (remove startup script mount, remove expose 9222). noVNC will still work as a plain desktop.
3. **If Playwright deps break the backend image**: Remove the `playwright install-deps` line from Dockerfile. Browser tool won't work but server.py will still serve non-browser requests.
4. **Full rollback**: Revert all changes to the previous commit. The system returns to the state described in [`CURRENT_TASK.md`](CURRENT_TASK.md) — working chat, no browser automation.

## Files Changed Summary

| File | Action | Description |
|------|--------|-------------|
| [`config.toml`](config.toml) | MODIFY | Add `[browser]` section with `cdp_url` |
| [`Dockerfile`](Dockerfile) | MODIFY | Add `playwright install-deps` for system libraries |
| [`docker-compose.yaml`](docker-compose.yaml) | MODIFY | Add Chromium install, expose 9222, startup script mount, depends_on |
| [`entrypoint.sh`](entrypoint.sh) | MODIFY | Add `BROWSER_CDP_URL` substitution |
| `novnc-startup.sh` | NEW | Script to launch Chromium with CDP in noVNC container |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| CDP connection refused (timing) | Medium | Agent can't browse | Add retry logic or health check wait |
| Webtop image doesn't have chromium in apt | Low | No browser in noVNC | Use `DOCKER_MODS` or manual install in startup script |
| Playwright Python bindings incompatible with remote CDP | Low | Browser tool fails | Test with `browser-use` library's CDP support first |
| noVNC container OOM with Chromium | Medium | Container crashes | Increase `shm_size` to 2gb, add memory limit |
| Fidelity blocks automated browsing | High | Can't download statements | User handles login/captcha via noVNC, agent does navigation |

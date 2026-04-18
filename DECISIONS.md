# Architecture Decisions Log

## Decision 2026-04-11-001: Use CMD instead of ENTRYPOINT in Dockerfile

**Context**: Coolify's docker-compose deployment engine overrides the Dockerfile `ENTRYPOINT` with an empty array `[]` and sets its own `CMD`. This caused [`entrypoint.sh`](entrypoint.sh) to never execute, meaning API key substitution into [`config.toml`](config.toml) never happened.

**Decision**: Change [`Dockerfile`](Dockerfile:11) from `ENTRYPOINT ["./entrypoint.sh"]` to `CMD ["./entrypoint.sh"]`. Additionally, set `entrypoint: ["./entrypoint.sh"]` explicitly in [`docker-compose.yaml`](docker-compose.yaml) as a belt-and-suspenders approach.

**Rationale**: `CMD` is less likely to be overridden by orchestrators than `ENTRYPOINT`. The explicit `entrypoint:` in docker-compose.yaml ensures Coolify uses it regardless of its default behavior.

**Alternatives considered**:
- Setting `start_command` in Coolify's application config via tinker — too fragile, not version-controlled
- Baking API keys into the image at build time — security risk, not 12-factor compliant

---

## Decision 2026-04-11-002: Add structlog as explicit dependency in Dockerfile

**Context**: The upstream [FoundationAgents/OpenManus](https://github.com/FoundationAgents/OpenManus) repo added `import structlog` in `app/utils/logger.py` but the package is not in `requirements.txt` (or was added after our clone point). Since the [`Dockerfile`](Dockerfile:4) does `git clone` at build time, we get whatever HEAD is at build time.

**Decision**: Add `pip install --no-cache-dir structlog` as a separate `RUN` step in the [`Dockerfile`](Dockerfile:9) after the main `requirements.txt` install.

**Rationale**: This is a targeted fix. The upstream repo's `requirements.txt` is unreliable since we clone HEAD. Adding structlog explicitly ensures it's always present.

**Future consideration**: Pin the upstream repo to a specific commit/tag to avoid surprise dependency changes.

---

## Decision 2026-04-11-003: Add missing env vars to docker-compose.yaml

**Context**: The [`docker-compose.yaml`](docker-compose.yaml) (used by Coolify) only passes `OPENAI_API_KEY`, but the application also requires `DAYTONA_API_KEY`. The local [`docker-compose.yaml`](docker-compose.yaml) has the correct set of env vars.

**Decision**: Update [`docker-compose.yaml`](docker-compose.yaml) to include `DAYTONA_API_KEY`, `OPENROUTER_API_KEY`, and `OPENAI_API_BASE_URL` environment variables, matching [`docker-compose.yaml`](docker-compose.yaml).

**Rationale**: Both compose files should be consistent. The Coolify-deployed version was missing critical env vars.

---

## Decision 2026-04-12-004: Create FastAPI server wrapper — OpenManus is a CLI, not an HTTP server

**Context**: The `openmanus-backend` container is crash-looping with `EOFError: EOF when reading a line` at [`main.py:20`](main.py). The upstream OpenManus project is a **CLI tool** — its `main.py` calls `input("Enter your prompt: ")` which immediately fails in a Docker container (no stdin/TTY). Meanwhile, `open-webui` expects an **OpenAI-compatible HTTP API** at `http://openmanus-backend:8000/v1`. OpenManus has no HTTP server mode — only CLI (`main.py`), MCP stdio server (`run_mcp_server.py`), and A2A protocol server (`protocol/a2a/app/main.py` on port 10000, not OpenAI-compatible).

**Decision**: Create a new [`server.py`](server.py) file — a lightweight FastAPI application that:
1. Exposes `/v1/chat/completions` (POST) and `/v1/models` (GET) in OpenAI-compatible format
2. Delegates to `app.agent.manus.Manus` agent internally
3. Runs on `0.0.0.0:8000` via uvicorn
4. Creates a fresh agent per request (agents are stateful, not thread-safe)
5. Includes `/health` endpoint for monitoring

Update [`entrypoint.sh`](entrypoint.sh) to run `python server.py` instead of `python main.py`. Update [`Dockerfile`](Dockerfile) to COPY `server.py` into the image. Remove the Tailscale volume mount from [`docker-compose.yaml`](docker-compose.yaml) (path doesn't exist on server, Coolify already ignores it).

**Rationale**: FastAPI and uvicorn are already installed in the image (dependencies of upstream OpenManus). This is the minimal-surface-area fix — we add one new file and change one line in entrypoint.sh. The A2A server was considered but rejected because open-webui speaks OpenAI API, not A2A protocol.

**Alternatives considered**:
- Using the A2A protocol server — rejected: open-webui doesn't speak A2A, would require modifying open-webui config
- Using LiteLLM proxy — rejected: adds another dependency and container, over-engineered for this use case
- Switching to a different backend that already has OpenAI-compatible API — rejected: we specifically want OpenManus agent capabilities

**Risk**: Creating an agent per request may be slow (agent initialization takes ~1s) and could leak memory. Future improvement: add agent pooling or caching.

**Requires**: New Docker image build + push. Coolify env var change alone is NOT sufficient.

---

## Decision 2026-04-12-005: Browser Automation via CDP to noVNC Container's Chromium

**Context**: User asked OpenManus to browse fidelity.com and got `RetryError[NotFoundError]`. Investigation revealed that while upstream OpenManus already includes [`BrowserUseTool`](https://github.com/FoundationAgents/OpenManus/blob/main/app/tool/browser_use_tool.py) (Playwright + browser-use library) wired into the [`Manus`](https://github.com/FoundationAgents/OpenManus/blob/main/app/agent/manus.py) agent, it fails because: (1) no Chromium binary is installed in the Docker image (`playwright install chromium` never runs), (2) no display server exists in the backend container for non-headless mode, and (3) no `[browser]` config section exists in [`config.toml`](config.toml).

**Decision**: Run Chromium inside the existing noVNC/webtop container (`lscr.io/linuxserver/webtop:ubuntu-mate`) with Chrome DevTools Protocol (CDP) enabled on port 9222. Configure OpenManus `BrowserUseTool` to connect via `cdp_url = "http://novnc:9223"` in the `[browser]` section of [`config.toml`](config.toml). Install only Playwright system dependencies (`playwright install-deps`) in the backend container — no browser binary needed since we connect remotely.

**Rationale**: This approach satisfies all user requirements simultaneously:
- **Shared visibility**: User sees the browser live in noVNC at `vnc.designflow.app`
- **Human-in-the-loop**: User can take over mouse/keyboard in noVNC (e.g., to log in to Fidelity)
- **Agent control**: OpenManus controls the same browser via CDP protocol
- **Minimal changes**: Uses existing upstream `BrowserUseTool` with its built-in `cdp_url` config support (see [`BrowserSettings.cdp_url`](https://github.com/FoundationAgents/OpenManus/blob/main/app/config.py) and [`_ensure_browser_initialized()`](https://github.com/FoundationAgents/OpenManus/blob/main/app/tool/browser_use_tool.py))
- **No new containers**: Reuses the existing noVNC container, just adds Chromium + CDP to it

**Alternatives considered**:
- **Headless Playwright in backend container only** — rejected: no shared visibility, no human-in-the-loop capability. User can't log in manually.
- **Separate Playwright MCP server container** — rejected: OpenManus already has `BrowserUseTool` built in; adding an MCP server is redundant complexity. MCP stdio transport doesn't work across containers.
- **browser-use with its own headless browser + VNC streaming** — rejected: over-engineered; the noVNC container already provides the desktop environment.
- **Daytona sandbox with VNC** — rejected: Daytona is not configured (`not-configured` placeholder), and adding it introduces a cloud dependency for something that can run locally.

**Risk**: CDP connection timing — if Chromium hasn't started in the noVNC container when the agent tries to connect, it will fail. Mitigation: add retry logic or a health check wait. Also, `browser-use` library's CDP support needs verification — it uses Playwright's `cdp_url` parameter which is well-documented.

**Requires**: Docker image rebuild (for `playwright install-deps`), updated `config.toml`, updated `docker-compose.yaml`, new `novnc-startup.sh` script. Full plan in [`implementation_plan.md`](implementation_plan.md).

---

## Decision 2026-04-18-006: Post-deploy smoke check for critical routes

**Context**: On 2026-04-18, the "Open Tool Manager" route at `https://manus.designflow.app/admin/tools` returned 502 Bad Gateway. Root cause was a stale Traefik dynamic config file at `/data/coolify/proxy/dynamic/tool-manager.yaml` that intercepted `/admin/*` and `/api/tools` and routed them to a dead upstream (`http://10.0.4.5:8000`). The stale config was external to this repo — no code change could have prevented it — but the outage went undetected because there was no automated verification after deployment.

**Decision**: Add a `smoke-check` job to [`.github/workflows/build-deploy.yml`](.github/workflows/build-deploy.yml:62) that runs after the `build-and-push` job succeeds. It waits 150s for Coolify to pull the new image and restart containers, then curls three critical routes through the public internet (same path as users, through the same Traefik proxy). Each route is retried up to 8 times with 20s delays.

**Rationale**: Catches stale proxy overrides immediately after deployment, creates a visible audit trail in GitHub Actions, and requires no server-side changes or new secrets.

**Limitations**:
- A smoke check failure does **not** take down the site — Docker's `restart: unless-stopped` controls uptime, not CI.
- Only catches failures on post-push deploys, not stale configs introduced between deployments.

---

## Decision 2026-04-18-007: pull_policy per service — always for backend, if_not_present for others

**Context**: Initially `pull_policy: always` was added to all services to prevent stale image caching. But this caused open-webui (90+ second startup) and novnc to be fully restarted on every openmanus-backend-only deploy, causing unnecessary outages.

**Decision**: 
- `openmanus-backend`: `pull_policy: always` — this is our image, always fetch the latest on deploy
- `open-webui`: `pull_policy: if_not_present` — third-party image, does not change on our pushes
- `novnc`: `pull_policy: if_not_present` — third-party image, does not change on our pushes

**Rationale**: With `if_not_present`, Docker only pulls the image on first run or after manual removal. Since these images use fixed tags (`:main`, `ubuntu-mate`), they don't auto-update — but we never need them to update on every deploy.

**Trade-off**: If you want to upgrade open-webui or novnc to a newer image version, you must either change the image tag in docker-compose.yaml or manually remove the image on the host and trigger a deploy.

---

## Decision 2026-04-18-008: CDP proxy for cross-container browser access

**Context**: Chrome binds its CDP debugger to 127.0.0.1 regardless of --remote-debugging-address. Other containers cannot reach it directly.

**Decision**: Create cdp_proxy.py — a hand-rolled HTTP+WS proxy listening on 0.0.0.0:9223 (HTTP) and 0.0.0.0:9224 (WS) inside the novnc container. It forwards requests to 127.0.0.1:9222 and rewrites response URLs so other containers connect via novnc:9224.

**Rationale**: Minimal code for a narrow use case. A library would be overkill. The backend connects via BROWSER_CDP_URL=http://novnc:9223.

---

## Decision 2026-04-18-009: Chromium autostart via MATE .desktop file, not direct launch

**Context**: novnc-startup.sh runs via LSIO's custom-cont-init.d mechanism, which executes BEFORE the X server starts. Direct Chromium launch (su -c 'DISPLAY=:1 chromium') fails with "Missing X server or $DISPLAY".

**Decision**: Create a MATE autostart .desktop file in /config/.config/autostart/ instead of launching Chromium directly. The script waits for CDP to become available (up to 120s).

**Rationale**: MATE autostart runs after the desktop is ready. This is the correct lifecycle for GUI apps in LSIO containers.

---

## Decision 2026-04-18-010: Rename branch master → main

**Context**: AI_OPERATING_RULES.md specifies single branch `main` but the repo used `master`. The workflow triggered on both, creating ambiguity.

**Decision**: Renamed GitHub branch from master to main. Updated workflow to branches: [main]. Updated Coolify app config git_branch to main.

---

## Decision 2026-04-18-011: Tool Manager UI embedded in server.py

**Context**: Users need a way to create, edit, and test custom Python tools without SSH or file uploads.

**Decision**: Embed the Tool Manager as a single-page HTML app in server.py (TOOL_MANAGER_HTML constant), served at /admin/tools. API endpoints at /api/tools/* handle CRUD and invocation. Tools are stored in /app/user_tools/ (persisted via Docker volume) and auto-loaded into every agent request.

**Rationale**: No build step, no extra container, no frontend framework. Pure HTML/JS with CodeMirror for syntax highlighting.

---

## Decision 2026-04-18-012: Traefik exact Path match for /admin/tools, not PathPrefix

**Context**: The original Traefik routing rule used `PathPrefix('/admin/')` to route requests to openmanus-backend. This silently hijacked all of OpenWebUI's own admin routes (`/admin/users`, `/admin/settings`, `/admin/functions`, etc.) — those paths were sent to the backend and returned 404 instead of reaching OpenWebUI. The OpenWebUI admin panel appeared to work (it loaded) but all API calls behind "Save" buttons returned 404, causing silent save failures.

**Decision**: Change the Traefik rule from `PathPrefix('/admin/')` to `Path('/admin/tools')` (exact match). The full rule is:

```
Host(`manus.designflow.app`) && (Path(`/admin/tools`) || PathPrefix(`/api/tools`) || PathPrefix(`/api/owui`))
```

**Rationale**: Only `/admin/tools` belongs to the Tool Manager. All other `/admin/*` paths are OpenWebUI's. An exact match is the safest possible scope. This is the kind of bug that's invisible in testing because the Tool Manager page itself loads fine — you only discover it when you try to use the OpenWebUI admin panel.

**Warning for future developers**: Do not use `PathPrefix('/admin/')` in any Traefik rule that routes to openmanus-backend. OpenWebUI's own admin API uses this prefix extensively.

---

## Decision 2026-04-18-013: No Docker healthcheck on openmanus-backend

**Context**: Coolify silently promotes `depends_on: service` to `depends_on: service: condition: service_healthy` whenever the dependency service has a Docker `HEALTHCHECK`. When a healthcheck was added to openmanus-backend and open-webui was configured to `depends_on` it, Coolify silently added `condition: service_healthy`. The healthcheck used `curl` (not present in the image), causing it to always fail. This prevented open-webui from ever starting.

**Decision**: Remove the Docker `HEALTHCHECK` from openmanus-backend entirely. The `/health` HTTP endpoint in `server.py` remains, but is not wired to Docker's health mechanism.

**Rationale**: The healthcheck → Coolify promotion → service_healthy dependency chain is a trap. The backend's `/health` endpoint is useful for external uptime monitoring but must not participate in Docker's startup dependency resolution. If you want to use `/health` for monitoring, use an external tool (UptimeRobot, Coolify's monitoring UI, etc.).

---

## Decision 2026-04-18-014: OpenWebUI sync uses browser session JWT, not stored API key

**Context**: The Tool Manager "Sync to OpenWebUI" feature originally stored an OpenWebUI API key in `/app/user_tools/.settings.json` and used it for all OWUI API calls. This required the user to manually generate an API key in OpenWebUI settings, copy it, and paste it into the Tool Manager settings panel. The API key storage was plaintext on disk.

**Decision**: Remove API key storage. The sync endpoints now read the session JWT from the user's browser cookie (`token` cookie, set by OpenWebUI on login) and pass it as `Authorization: Bearer <token>` to OpenWebUI's API. Helper function signature: `_session_token(request: Request) -> str` reads `request.cookies.get("token", "")`.

**Rationale**: The Tool Manager is served from `manus.designflow.app`, the same domain as OpenWebUI. The browser already has a valid session cookie. Using it directly eliminates the need for API key management, works immediately after the user logs in, and requires no additional configuration.

**Implication for developers**: All five `/api/owui/*` endpoints require the request object to extract the token. If the user is not logged in (no cookie), the sync returns HTTP 401. The settings panel no longer has an API key field — any `.settings.json` files with a leftover `api_key` field are harmless but ignored.

---

## Decision 2026-04-18-015: OpenWebUI 0.8.x /id/ prefix required for per-resource API routes

**Context**: The OWUI sync was returning HTTP 405 "Method Not Allowed" when trying to update existing tools. Root cause: OpenWebUI 0.8.x changed its per-resource API routes to require an `/id/` prefix. The paths `/api/v1/tools/{id}` (GET) and `/api/v1/tools/{id}/update` (POST) do NOT work — they hit the SPA router which returns 200 HTML for GET (making existence checks always return true) and 405 for POST.

**Decision**: Use the `/id/` prefix for all per-resource OpenWebUI API calls:
- `GET /api/v1/tools/id/{id}` — check existence
- `POST /api/v1/tools/id/{id}/update` — update existing tool
- `DELETE /api/v1/tools/id/{id}/delete` — delete tool
- `GET /api/v1/functions/id/{fn_id}` — check function existence
- `POST /api/v1/functions/id/{fn_id}/update` — update existing function

Create and list endpoints do NOT use `/id/`: `POST /api/v1/tools/create`, `GET /api/v1/tools/`.

**Warning for future developers**: This is not documented in the OpenWebUI source in an obvious place. If you see sync calls returning 405 or existence checks always returning true, check for missing `/id/` prefix. This was verified against OpenWebUI 0.8.12.

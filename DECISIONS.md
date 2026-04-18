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

**Context**: The [`docker-compose.yaml`](docker-compose.yaml) (used by Coolify) only passes `OPENAI_API_KEY`, but the application also requires `DAYTONA_API_KEY`. The local [`docker-compose.yml`](docker-compose.yml) has the correct set of env vars.

**Decision**: Update [`docker-compose.yaml`](docker-compose.yaml) to include `DAYTONA_API_KEY`, `OPENROUTER_API_KEY`, and `OPENAI_API_BASE_URL` environment variables, matching [`docker-compose.yml`](docker-compose.yml).

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

**Decision**: Run Chromium inside the existing noVNC/webtop container (`lscr.io/linuxserver/webtop:ubuntu-mate`) with Chrome DevTools Protocol (CDP) enabled on port 9222. Configure OpenManus `BrowserUseTool` to connect via `cdp_url = "http://novnc:9222"` in the `[browser]` section of [`config.toml`](config.toml). Install only Playwright system dependencies (`playwright install-deps`) in the backend container — no browser binary needed since we connect remotely.

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

**Decision**: Add a `smoke-check` job to [`.github/workflows/build-deploy.yml`](.github/workflows/build-deploy.yml:62) that runs after the `build-and-push` job succeeds. It waits 90s for Coolify to pull the new image and restart containers, then curls three critical routes through the public internet (same path as users, through the same Traefik proxy):

- `/admin/tools` — the Tool Manager UI (the route that broke)
- `/api/tools` — the Tool Manager API (also intercepted by the stale config)
- `/` — the Open WebUI homepage (baseline sanity check)

Each route is retried up to 5 times with 15s delays. If any route fails to return 2xx after all retries, the job fails with a GitHub Actions `::error::` annotation, making the failure visible in the Actions UI and any connected notifications.

**Rationale**: This is the smallest solid repo-based safeguard because:
- It catches stale proxy overrides immediately after deployment, before users discover them
- It creates a visible audit trail in GitHub Actions (red ❌ on the workflow run)
- It requires no server-side changes, no new infrastructure, and no new secrets
- It cannot be bypassed by external runtime state — it checks from the outside, exactly as a user would
- The retry logic handles normal deployment rollouts where containers take time to start

**Alternatives considered**:
- **Traefik dynamic config validation on the server** — rejected: requires SSH access in CI (security risk), and the stale config is outside this repo's control
- **Health check endpoint in the app** — rejected: the app was healthy; the proxy was the problem. An internal health check would have passed.
- **Coolify API polling for deployment status** — rejected: Coolify reports deployment success even when proxy routes are broken (the container started fine)
- **Scheduled uptime monitor (e.g., UptimeRobot)** — rejected: out of scope for this repo; that's an operational tool, not a repo-based safeguard. Could be added separately.

**Limitations**:
- The 90s settle time is a best guess; very slow deployments may not be caught on the first attempt (but retries help)
- The smoke check only runs after pushes to main; it won't catch stale configs introduced between deployments
- It does not prevent stale configs from being created; it only detects the symptom (non-2xx responses)
- If `SMOKE_BASE_URL` is wrong or DNS is misconfigured, the check will false-positive fail

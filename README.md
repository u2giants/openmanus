# OpenManus — AI Agent Stack

A 3-container Docker stack that gives OpenManus AI agents a real browser they can control via Chrome DevTools Protocol (CDP), with a shared desktop the user can watch and interact with live.

## Live URLs

| Service | URL |
|---------|-----|
| Open WebUI (chat) | <https://manus.designflow.app> |
| noVNC desktop (watch the browser) | <https://vnc.designflow.app> |

## Architecture

```
┌──────────────┐     OpenAI-compatible API      ┌──────────────────┐
│  Open WebUI  │ ──── POST /v1/chat/completions ──▶│ OpenManus Backend│
│  :3000       │ ◀─── streaming SSE response ─────│  :8000           │
└──────────────┘                                   └────────┬─────────┘
                                                            │
                                                     Manus agent calls
                                                     BrowserUseTool
                                                            │
                                                     CDP via HTTP+WS
                                                     (novnc:9223/9224)
                                                            │
                                                            ▼
                                                   ┌──────────────────┐
                                                   │  noVNC Container │
                                                   │  Chromium + CDP  │
                                                   │  :9222 (local)   │
                                                   │  :9223 (proxy)   │
                                                   │  :9224 (WS proxy)│
                                                   └──────────────────┘
```

**Flow**: User chats in Open WebUI → OpenManus API receives the prompt → Manus agent decides which tools to use → if browser automation is needed, it connects to Chromium in the noVNC container via CDP proxy → user can watch and interact with the same browser at `vnc.designflow.app`.

## Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `openmanus-backend` | `ghcr.io/u2giants/openmanus:main` | 8001→8000 | FastAPI server wrapping OpenManus agent with OpenAI-compatible API |
| `open-webui` | `ghcr.io/open-webui/open-webui:main` | 3000→8080 | Chat frontend with Google SSO, tool manager UI |
| `novnc` | `lscr.io/linuxserver/webtop:ubuntu-mate` | 3000 (web) | Remote desktop with Chromium browser + CDP proxy |

## Key Files

| File | Purpose |
|------|---------|
| [`Dockerfile`](Dockerfile) | Builds the OpenManus backend image — clones upstream, installs deps, copies custom files |
| [`entrypoint.sh`](entrypoint.sh) | Container startup — substitutes env vars into `config.toml`, then runs `server.py` |
| [`server.py`](server.py) | FastAPI app — OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`), Tool Manager UI (`/admin/tools`), Tool Manager API (`/api/tools/*`) |
| [`config.toml`](config.toml) | OpenManus runtime config — LLM settings, browser CDP URL, Daytona key (env vars substituted at startup) |
| [`docker-compose.yaml`](docker-compose.yaml) | 3-service compose — backend, Open WebUI, noVNC with Traefik labels |
| [`novnc-startup.sh`](novnc-startup.sh) | noVNC init — creates MATE autostart `.desktop` for Chromium, waits for CDP, starts CDP proxy |
| [`cdp_proxy.py`](cdp_proxy.py) | HTTP+WS proxy — forwards CDP from `127.0.0.1:9222` to `0.0.0.0:9223` (HTTP) and `0.0.0.0:9224` (WS), rewrites URLs for cross-container access |

## Deployment Pipeline

```
git push → GitHub Actions → Docker build → Push to GHCR → Coolify webhook → Coolify pulls new image → Containers restart
```

1. **Push to `main`** triggers [`.github/workflows/build-deploy.yml`](.github/workflows/build-deploy.yml)
2. **Build job** builds the Docker image and pushes to `ghcr.io/u2giants/openmanus:main`
3. **Deploy step** calls the Coolify webhook to trigger a redeployment
4. **Smoke check job** waits 90s, then curls `/admin/tools`, `/api/tools`, and `/` to verify the deployment is healthy
5. Coolify pulls the new image (via `pull_policy: always`) and restarts containers

## Custom Tools

User-defined Python tools are stored in `/app/user_tools/` inside the backend container, persisted via a Docker volume (`/data/coolify/openmanus/user_tools`).

### Tool Manager UI

A single-page HTML app embedded in [`server.py`](server.py) at the `TOOL_MANAGER_HTML` constant, served at `/admin/tools`. It provides:

- Create, edit, and delete Python tool files
- Syntax highlighting via CodeMirror
- Test tools by invoking them directly from the UI
- API endpoints at `/api/tools/*` handle CRUD and invocation

Tools are auto-loaded into every agent request — the backend scans `/app/user_tools/` at startup and on each request.

### How to Add a Custom Tool

1. Go to <https://manus.designflow.app/admin/tools>
2. Click **New Tool**
3. Write a Python file with a function decorated appropriately (e.g., `@tool` or a plain function the agent can call)
4. Save — the tool is immediately available to the agent
5. Alternatively, place a `.py` file directly in `/app/user_tools/` on the server

## OpenWebUI Functions

Two custom functions are served at `/admin/functions/`:

- **Run Tool** — executes a tool directly from the OpenWebUI interface
- **Save to Knowledge** — saves conversation output to the OpenWebUI knowledge base for RAG

## Google SSO

Enabled via Open WebUI's built-in OAuth support. Configured in [`docker-compose.yaml`](docker-compose.yaml) with:

- `ENABLE_OAUTH_SIGNUP=true`
- `OAUTH_PROVIDER_NAME=Google`
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (from env vars)
- `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`
- `OPENID_PROVIDER_URL=https://accounts.google.com/.well-known/openid-configuration`

## How to Add a New Decision to DECISIONS.md

1. Open [`DECISIONS.md`](DECISIONS.md)
2. Append a new section using the format:

```markdown
## Decision YYYY-MM-DD-NNN: Short title

**Context**: What situation prompted this decision.

**Decision**: What was decided.

**Rationale**: Why this approach was chosen.

**Alternatives considered**: (optional) What else was evaluated.

---
```

3. Increment the decision number (NNN) sequentially
4. Commit with a descriptive message

## Project Structure

```
.
├── .github/workflows/build-deploy.yml   # CI/CD — build, push, deploy, smoke check
├── cdp_proxy.py                         # CDP HTTP+WS proxy for cross-container browser access
├── config.toml                          # OpenManus runtime config (env vars substituted at startup)
├── custom_tools/                        # Custom tool .py files copied into the Docker image
├── DECISIONS.md                         # Architecture decision log
├── docker-compose.yaml                  # 3-service compose definition
├── Dockerfile                           # Backend image build
├── entrypoint.sh                        # Container startup script
├── novnc-startup.sh                     # noVNC init — Chromium autostart + CDP proxy
├── server.py                            # FastAPI server + Tool Manager UI
└── TROUBLESHOOTING.md                   # Operational troubleshooting guide
```

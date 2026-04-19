# OpenManus — AI Agent Stack

A 3-container Docker stack that gives OpenManus AI agents a real browser they can control via Chrome DevTools Protocol (CDP), with a shared desktop the user can watch and interact with live.

## Live URLs

| Service | URL |
|---------|-----|
| Open WebUI (chat) | <https://manus.designflow.app> |
| Tool Manager | <https://manus.designflow.app/admin/tools> |
| noVNC desktop (watch the browser) | <https://vnc.designflow.app> |

## ClawdTalk

The backend now has an opt-in ClawdTalk bridge for phone/SMS-style voice sessions. When `CLAWDTALK_API_KEY` is set, `server.py` opens a persistent outbound WebSocket to ClawdTalk, routes inbound `message` events through the existing Manus agent path, and replies with ClawdTalk `response` frames. The bridge keeps short per-call conversation history so a live call has context across turns.

Environment variables on `openmanus-backend`:

| Variable | Default | Purpose |
|---------|---------|---------|
| `CLAWDTALK_API_KEY` | unset | Enables the bridge and authenticates with ClawdTalk |
| `CLAWDTALK_SERVER_URL` | `https://clawdtalk.com` | Base URL for REST calls like outbound call creation |
| `CLAWDTALK_WS_URL` | derived from server URL | Explicit WebSocket URL override if ClawdTalk assigns a different endpoint |
| `CLAWDTALK_AGENT_NAME` | `OpenManus` | Name presented to ClawdTalk for this agent |
| `CLAWDTALK_OWNER_NAME` | unset | Optional caller/owner hint used in the voice prompt |
| `CLAWDTALK_GREETING` | unset | Optional first-turn greeting/instruction for voice calls |

Operational endpoints:

- `GET /api/clawdtalk/status` — current bridge state, connection status, last error, active calls
- `POST /api/clawdtalk/calls` — proxy to ClawdTalk outbound call creation; body is forwarded to `CLAWDTALK_SERVER_URL/v1/calls`

## Architecture

```
┌──────────────┐     OpenAI-compatible API      ┌──────────────────────┐
│  Open WebUI  │ ──── POST /v1/chat/completions ──▶│  OpenManus Backend   │
│  :3000/8080  │ ◀─── streaming SSE response ─────│  :8000               │
└──────────────┘                                   └────────┬─────────────┘
       │                                                    │
  /admin/tools ──── Traefik ──────────────────────────────▶│ /admin/tools (Tool Manager UI)
  /api/tools/* ────────────────────────────────────────────▶│ /api/tools/* (Tool CRUD API)
  /api/owui/*  ────────────────────────────────────────────▶│ /api/owui/*  (OpenWebUI sync)
                                                            │
                                                     Manus agent calls
                                                     BrowserUseTool
                                                            │
                                                     CDP via HTTP+WS
                                                     (novnc:9223/9224)
                                                            │
                                                            ▼
                                               ┌──────────────────────┐
                                               │   noVNC Container    │
                                               │   Chromium + CDP     │
                                               │   :9222 (local)      │
                                               │   :9223 (HTTP proxy) │
                                               │   :9224 (WS proxy)   │
                                               └──────────────────────┘
```

**Flow**: User chats in Open WebUI → OpenManus API receives the prompt → Manus agent decides which tools to use → if browser automation is needed, it connects to Chromium in the noVNC container via CDP proxy → user can watch and interact with the same browser at `vnc.designflow.app`.

## Services

| Service | Image | External Port | Purpose |
|---------|-------|---------------|---------|
| `openmanus-backend` | `ghcr.io/u2giants/openmanus:main` | 8001→8000 | FastAPI server wrapping OpenManus agent with OpenAI-compatible API + Tool Manager |
| `open-webui` | `ghcr.io/open-webui/open-webui:main` | 3000→8080 | Chat frontend with Google SSO |
| `novnc` | `lscr.io/linuxserver/webtop:ubuntu-mate` | (internal) | Remote desktop with Chromium browser + CDP proxy |

## Traefik Routing

All traffic enters via Traefik on `manus.designflow.app`. More-specific rules win.

| Rule | Routes to | Why |
|------|-----------|-----|
| `Path('/admin/tools')` | `openmanus-backend:8000` | Tool Manager UI — exactly this one path |
| `PathPrefix('/api/tools')` | `openmanus-backend:8000` | Tool CRUD and invocation API |
| `PathPrefix('/api/owui')` | `openmanus-backend:8000` | OpenWebUI sync API |
| `PathPrefix('/api/clawdtalk')` | `openmanus-backend:8000` | ClawdTalk bridge status + outbound call API |
| `Host('manus.designflow.app')` catch-all | `open-webui:8080` | Everything else (chat, OpenWebUI admin, etc.) |
| `Host('vnc.designflow.app')` | `novnc:3000` | Desktop viewer |

**Important**: The backend rule uses `Path('/admin/tools')` (exact match), NOT `PathPrefix('/admin/')`. Using a prefix would hijack OpenWebUI's own `/admin/users`, `/admin/settings`, etc., breaking the OpenWebUI admin panel entirely.

## Key Files

| File | Purpose |
|------|---------|
| [`Dockerfile`](Dockerfile) | Builds the OpenManus backend image — clones upstream, installs deps, copies custom files |
| [`entrypoint.sh`](entrypoint.sh) | Container startup — substitutes env vars into `config.toml`, then runs `server.py` |
| [`server.py`](server.py) | FastAPI app — OpenAI API, Tool Manager UI + API, OpenWebUI sync endpoints |
| [`config.toml`](config.toml) | OpenManus runtime config — LLM settings, browser CDP URL (env vars substituted at startup) |
| [`docker-compose.yaml`](docker-compose.yaml) | 3-service compose definition with Traefik labels |
| [`novnc-startup.sh`](novnc-startup.sh) | noVNC init — creates MATE autostart `.desktop` for Chromium, waits for CDP, starts CDP proxy |
| [`cdp_proxy.py`](cdp_proxy.py) | HTTP+WS proxy — forwards CDP from `127.0.0.1:9222` to `0.0.0.0:9223/9224`, rewrites URLs |

## Deployment Pipeline

```
git push main
  → GitHub Actions builds Docker image
  → pushes to ghcr.io/u2giants/openmanus:main
  → PATCHes Coolify DB with new docker-compose.yaml (base64-encoded)
  → POSTs Coolify deploy webhook
  → Coolify pulls new openmanus-backend image and restarts it
  → Smoke check verifies /admin/tools, /api/tools, /
```

**Critical**: Coolify deploys from its own internal database copy of the compose, not directly from GitHub. The CI workflow includes a PATCH step to sync the repo's `docker-compose.yaml` into Coolify's DB before triggering the deploy. If you change the compose and skip this step (e.g., by triggering Coolify manually without a CI run), Coolify will deploy from its stale DB copy.

**Coolify service UUID**: `e10kwzww46ljhrgz1qj08j6a`  
**Coolify API base**: `http://178.156.180.212:8000/api/v1`

### pull_policy design

- `openmanus-backend`: `pull_policy: always` — this is our image, always fetch the latest on deploy
- `open-webui` and `novnc`: `pull_policy: if_not_present` — these images don't change on our pushes; always-pulling them would force a full container restart (90+ second outage) on every deploy for no reason

### Smoke check

After each deploy the CI waits 150s (for `open-webui` to initialize) then curls three routes up to 8 times with 20s between retries. A smoke check failure means a route returned non-2xx — it does **not** take the site down. The site is controlled by Docker's `restart: unless-stopped`, not by CI.

## Custom Tools (user_tools)

User-defined Python tools are stored in `/app/user_tools/` (persisted via bind mount to `/data/coolify/openmanus/user_tools` on the host).

### Tool structure

Each `.py` file must contain one or more `BaseTool` subclasses (from `app.tool.base`):

```python
from app.tool.base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "Does something useful."
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "The input value"}
        },
        "required": ["input"]
    }

    async def execute(self, input: str) -> str:
        return f"Result: {input}"
```

### Tool Manager UI

Served at `/admin/tools`. Provides:
- Create, edit, delete Python tool files with syntax highlighting (CodeMirror)
- Invoke tools directly from the UI to test them
- Sync tools to OpenWebUI's native tool registry (so they appear in chat)

### OpenWebUI Sync

The Tool Manager can sync user tools into OpenWebUI's native tool registry. When synced, tools appear in chat via the ⚡ tool selector. The sync uses the user's existing OpenWebUI session JWT (from the browser cookie — no API key needed):

1. Open Tool Manager → **⚙ Settings** → **⚡ Auto-connect** — verifies your session works
2. **↻ Sync All** — registers all tools in OpenWebUI's tool registry

The sync generates a proxy stub class in OpenWebUI's format that calls back to `http://openmanus-backend:8000/api/tools/{name}/invoke`. Settings (OWUI URL) are stored in `/app/user_tools/.settings.json`.

**OpenWebUI API idiosyncrasy**: In OpenWebUI 0.8.x, per-resource routes use an `/id/` prefix. The correct paths are `/api/v1/tools/id/{id}`, `/api/v1/tools/id/{id}/update`, etc. — NOT `/api/v1/tools/{id}`. Without the `/id/` prefix, the SPA catches the request and returns 200 HTML, making existence checks always return true and causing update calls to return 405.

### Tool invocation in agent requests

When a user sends a chat message, `server.py` reads the `tools` array from the OpenAI-format request body. If the user selected specific tools via ⚡ in OpenWebUI, only those tools are injected into the agent's tool list. If no tools are selected (empty array), all user tools are injected — backwards-compatible with clients that don't send tool selection.

### Settings file

`/app/user_tools/.settings.json` stores:
- `owui_url` — OpenWebUI internal URL (default: `http://open-webui:8080`)
- `synced_tools` — list of tool stems successfully synced to OpenWebUI

The `api_key` field is intentionally absent — earlier versions stored an API key here, but the current implementation uses the browser session JWT directly. Any `.settings.json` files with an `api_key` field are harmless but the value is ignored.

## Google SSO

Configured via Open WebUI's built-in OAuth support:

- `ENABLE_OAUTH_SIGNUP=true`
- `OAUTH_PROVIDER_NAME=Google`
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — set in Coolify UI (not in this repo)
- `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` — Google and local accounts with the same email merge
- `OPENID_PROVIDER_URL=https://accounts.google.com/.well-known/openid-configuration`

The 401 toast on the login page is normal — OpenWebUI fires a session check before the page loads, which correctly returns 401 for unauthenticated users.

## Custom Souls

A volume mount at `/mnt/tailscale/souls:/app/custom_souls` makes custom agent "soul" definitions available inside the container. This path comes from Tailscale network storage on the host; if the path doesn't exist, Docker will create an empty directory silently.

## Project Structure

```
.
├── .github/workflows/build-deploy.yml   # CI/CD — build, push, sync compose to Coolify, deploy, smoke check
├── cdp_proxy.py                         # CDP HTTP+WS proxy for cross-container browser access
├── config.toml                          # OpenManus runtime config (env vars substituted at startup)
├── custom_tools/                        # Tool .py files baked into the Docker image at build time
├── AI_OPERATING_RULES.md                # Rules for AI assistants working on this repo
├── COOLIFY_APP_STRUCTURE.md             # How the stack is organized in Coolify
├── DECISIONS.md                         # Architecture decision log
├── docker-compose.yaml                  # 3-service compose definition
├── Dockerfile                           # Backend image build
├── entrypoint.sh                        # Container startup script
├── novnc-startup.sh                     # noVNC init — Chromium autostart + CDP proxy
├── README.md                            # This file
├── server.py                            # FastAPI server + Tool Manager UI + OpenWebUI sync
└── TROUBLESHOOTING.md                   # Operational troubleshooting guide
```

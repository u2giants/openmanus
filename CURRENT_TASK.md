# Current Task: OpenManus + Open WebUI + noVNC Deployment

## Status: COMPLETE ✅

### Post-Deploy Review Update

- Browser automation implementation review is in progress.
- Reviewing [`novnc-startup.sh`](novnc-startup.sh), [`config.toml`](config.toml), [`Dockerfile`](Dockerfile), [`entrypoint.sh`](entrypoint.sh), and [`docker-compose.yaml`](docker-compose.yaml).
- Git push will only happen after review passes.

**Live URL: https://manus.designflow.app**
**noVNC Desktop: https://vnc.designflow.app**
**Google SSO: Enabled (Continue with Google)**

---

## What Was Built

A fully automated CI/CD pipeline deploying a three-container AI agent stack with Google OAuth SSO and remote desktop:

| Service | Image | URL |
|---------|-------|-----|
| Open WebUI (frontend) | `ghcr.io/open-webui/open-webui:main` | https://manus.designflow.app |
| OpenManus backend (API) | `ghcr.io/u2giants/openmanus:main` | http://178.156.180.212:8001 |
| noVNC Desktop (webtop) | `lscr.io/linuxserver/webtop:ubuntu-mate` | https://vnc.designflow.app |

---

## CI/CD Pipeline (Fully Operational)

Every push to `master` on [`u2giants/openmanus`](https://github.com/u2giants/openmanus):
1. GitHub Actions builds the Docker image
2. Pushes to GHCR as `ghcr.io/u2giants/openmanus:main` and `ghcr.io/u2giants/openmanus:sha-<commit>`
3. Triggers Coolify webhook (HTTP 200) with `Authorization: Bearer` token
4. Coolify pulls the new image and redeploys both containers

---

## Architecture

### [`Dockerfile`](Dockerfile)
- Clones upstream `FoundationAgents/OpenManus` at build time
- Overlays `custom_tools/`, `config.toml`, `entrypoint.sh`, `server.py`
- Installs `structlog`, `daytona-sdk`, `fastapi`, `uvicorn[standard]` on top of upstream requirements
- Includes a `daytona` shim for import compatibility

### [`server.py`](server.py)
- OpenAI-compatible HTTP API wrapping the OpenManus `Manus` agent
- Exposes `/health`, `/v1/models`, `/v1/chat/completions` on port 8000
- Open WebUI connects to this at `http://openmanus-backend:8000/v1`

### [`entrypoint.sh`](entrypoint.sh)
- Substitutes `__OPENAI_API_KEY__` and `__DAYTONA_API_KEY__` placeholders in `config.toml` at startup
- Launches `python server.py` via uvicorn

### [`config.toml`](config.toml)
- LLM backend: OpenRouter (`https://openrouter.ai/api/v1`)
- Model: `openai/gpt-4o`
- API key injected at runtime from env var

### [`docker-compose.yaml`](docker-compose.yaml)
- Used by Coolify for deployment
- Backend: Tailscale volume mount `/mnt/tailscale/souls:/app/custom_souls`
- Frontend: Google OAuth SSO env vars, Traefik labels for `manus.designflow.app` with Let's Encrypt SSL

---

## GitHub Secrets (u2giants/openmanus)

| Secret | Purpose |
|--------|---------|
| `CR_PAT` | GHCR push access |
| `COOLIFY_WEBHOOK_URL` | Coolify deploy endpoint |
| `COOLIFY_API_TOKEN` | Coolify API authentication |
| `OPENAI_API_KEY` | OpenRouter API key (used as OpenAI key) |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENAI_API_BASE_URL` | `https://openrouter.ai/api/v1` |
| `DAYTONA_API_KEY` | `not-configured` (Daytona not in use) |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID for SSO |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret for SSO |

---

## Infrastructure

- **Coolify server**: `https://178.156.180.212`
- **Coolify resource UUID**: `openmanus-f9397c334d525e3ba812`
- **Cloudflare zone**: `designflow.app` (zone ID: `921eb133a3f7d5802780445b283f84ce`)
- **DNS**: `manus.designflow.app` A record → `178.156.180.212` (DNS only, no proxy)
- **SSL**: Let's Encrypt via Coolify/Traefik
- **Google OAuth redirect URI**: `https://manus.designflow.app/oauth/google/callback`

---

## Key Decisions (see [`DECISIONS.md`](DECISIONS.md))
- Use `CMD` not `ENTRYPOINT` in Dockerfile (Coolify overrides ENTRYPOINT)
- Use `server.py` (FastAPI/uvicorn HTTP server) not `main.py` (CLI tool)
- OpenRouter used as LLM backend; `OPENAI_API_KEY` holds the OpenRouter key
- Daytona not configured (`not-configured` placeholder)
- Google OAuth SSO enabled via Open WebUI's built-in OAuth support

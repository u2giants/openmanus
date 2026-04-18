# Coolify App Structure

How the OpenManus stack is organized on the Coolify server, and how the deployment paths relate to each other.

---

## Stack Identity

| Field | Value |
|-------|-------|
| Coolify service UUID | `e10kwzww46ljhrgz1qj08j6a` |
| Coolify API base | `http://178.156.180.212:8000/api/v1` |
| Stack type | **Service** (Docker Compose) — not "Application" |
| GHCR image | `ghcr.io/u2giants/openmanus:main` |

**Service vs Application type**: Coolify has two resource types — "Application" (single container, Nixpacks/Dockerfile build) and "Service" (Docker Compose, multi-container). The OpenManus stack is a Service. In the past, a stale "Application" entry existed in Coolify alongside the real Service; this caused confusion (3 red entries + 1 green). The Application entry was deleted — only the Service entry should exist.

---

## Directory Layout

```
/worksp/openmanus/
└── app/                          # git clone of the source repo (source of truth)
    ├── Dockerfile
    ├── server.py
    ├── entrypoint.sh
    ├── config.toml
    ├── novnc-startup.sh
    ├── cdp_proxy.py
    ├── docker-compose.yaml
    └── ...

/data/coolify/
├── services/e10kwzww46ljhrgz1qj08j6a/   # Coolify's working copy of the compose stack
│   ├── docker-compose.yml               # The compose file Coolify actually executes (NOT the repo copy)
│   ├── novnc-startup.sh                 # Synced from the repo at deploy time
│   └── cdp_proxy.py                     # Synced from the repo at deploy time
└── proxy/
    └── dynamic/                          # Traefik dynamic config — can cause stale routes if files linger here
```

---

## Critical: Coolify Deploys from its DB, Not from GitHub

Coolify stores its own copy of the compose file in its database. When a deploy is triggered, Coolify reads from its DB — **not** from the GitHub repo and not from `/data/coolify/services/<uuid>/docker-compose.yml` directly.

This means:
- If you change `docker-compose.yaml` in the repo and just trigger a Coolify deploy (without syncing the compose), Coolify deploys the old compose.
- The CI workflow handles this with a PATCH step: it base64-encodes the repo's `docker-compose.yaml` and PATCHes it into Coolify's DB before triggering the deploy.
- If you ever trigger a Coolify deploy manually (via the UI or API) without going through CI, your compose changes will **not** be picked up.

---

## How Volume Mounts Work

The `docker-compose.yaml` in the git repo references files like `./novnc-startup.sh` and `./cdp_proxy.py` as bind mounts:

```yaml
volumes:
  - ./novnc-startup.sh:/custom-cont-init.d/99-start-chromium.sh:ro
  - ./cdp_proxy.py:/custom-cont-init.d/cdp_proxy.py:ro
```

These relative paths resolve relative to the **Coolify service directory** (`/data/coolify/services/e10kwzww46ljhrgz1qj08j6a/`), not the git repo. Coolify copies the files from the repo to the service directory during deployment.

If the files are missing from the service directory, Docker will create **directories** at the mount points instead of files, causing the scripts to fail silently. See [TROUBLESHOOTING.md — Volume Mounts Created as Directories](TROUBLESHOOTING.md) for the fix.

---

## Traefik Dynamic Config Warning

Files in `/data/coolify/proxy/dynamic/` are loaded by Traefik as live routing rules and **override** the container-label-based routing. Stale files here can cause routes to go to dead upstreams even when the containers are healthy.

On 2026-04-18 a file `tool-manager.yaml` in this directory routed `/admin/*` to a dead IP, causing `/admin/tools` to return 502 while the container was running fine. The CI smoke check now catches this.

**If you add any file to this directory manually, document it in this file and in the repo.** Never create undocumented dynamic configs on the server.

---

## Coolify's Automatic `depends_on` Promotion

When a service has a Docker `HEALTHCHECK` and another service `depends_on` it, Coolify silently rewrites:

```yaml
depends_on:
  - openmanus-backend
```

to:

```yaml
depends_on:
  openmanus-backend:
    condition: service_healthy
```

This means: if the healthcheck is failing (even during startup), the dependent service never starts. The openmanus-backend has **no Docker healthcheck** specifically to avoid this. Do not add one. The `/health` HTTP endpoint in `server.py` exists for external monitoring only.

---

## Environment Variables

Runtime secrets are stored in Coolify's UI (not in this repo). The compose references them via `${VAR_NAME}` syntax. Current env vars:

| Variable | Set in | Notes |
|----------|--------|-------|
| `OPENAI_API_KEY` | Coolify | Used by OpenManus agent |
| `OPENROUTER_API_KEY` | Coolify | Primary LLM provider |
| `DAYTONA_API_KEY` | Coolify | Reserved for Daytona sandbox (not yet active) |
| `OPENAI_API_BASE_URL` | Coolify | Defaults to `https://openrouter.ai/api/v1` |
| `GOOGLE_CLIENT_ID` | Coolify | Google OAuth for Open WebUI |
| `GOOGLE_CLIENT_SECRET` | Coolify | Google OAuth for Open WebUI |

---

## Persistent Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `open-webui-data` (named) | `/app/backend/data` in open-webui | OpenWebUI SQLite DB, user accounts, settings |
| `novnc-data` (named) | `/config` in novnc | MATE desktop config, Chromium profile |
| `/data/coolify/openmanus/user_tools` (bind) | `/app/user_tools` in backend | User-defined Python tools (persisted across redeploys) |
| `/mnt/tailscale/souls` (bind) | `/app/custom_souls` in backend | Custom agent soul definitions (from Tailscale storage) |

User tools survive redeploys because they're on a host bind mount, not the Docker image. The Docker image bakes in `custom_tools/` (separate from `user_tools/`).

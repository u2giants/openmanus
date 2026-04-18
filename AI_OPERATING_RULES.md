# AI Operating Rules

## Purpose

These rules exist so AI tools can safely assist with this repo without creating production drift.

## System of truth

- GitHub is the source of truth for code, Docker Compose, Dockerfiles, and workflows.
- Coolify is the source of truth for production runtime environment variables and deployment target settings.
- The production server is only a runtime host, not a configuration source.

## Branch policy

- This repo uses one branch only: `main`
- Do not propose or create feature branches
- Do not suggest branch-based workflows
- Do not assume there is a staging branch
- All approved changes should target `main`

## Approved deployment path

The only normal deployment path is:

1. Change files in this repo
2. Commit to `main`
3. GitHub Actions builds and pushes the Docker image to GHCR
4. GitHub Actions PATCHes the Coolify DB with the new `docker-compose.yaml` (base64-encoded)
5. GitHub Actions triggers Coolify deploy webhook
6. Coolify deploys the new image

**Critical**: Steps 4 and 5 are both required. Coolify deploys from its own internal database copy of the compose file, NOT from GitHub. If you trigger Coolify manually (step 5) without first syncing the compose (step 4), Coolify will deploy from its stale DB copy, ignoring any compose changes.

**Coolify service UUID**: `e10kwzww46ljhrgz1qj08j6a`  
**Coolify API base**: `http://178.156.180.212:8000/api/v1`

Do not propose alternate routine deployment methods.

## Allowed AI actions

AI may help with:

- editing application code
- editing `docker-compose.yaml`
- editing Dockerfiles
- editing GitHub Actions workflows
- editing documentation
- recommending GitHub Secrets usage for CI/CD
- recommending Coolify runtime environment variable changes
- triggering deployment through the approved GitHub → Coolify path

## Forbidden AI actions

AI must not:

- use SSH as the normal deployment path
- hand-edit files directly on the production server
- assume the server contains the source of truth
- create undocumented hotfixes on the live machine
- introduce additional branches
- create a second deployment system
- recommend storing production runtime configuration only in ad hoc server files
- create Traefik dynamic config files or reverse-proxy overrides on the production server without committing them to the repo

## Secrets rule

- GitHub Secrets are for CI/CD and build-time secrets
- Coolify stores production runtime environment variables
- Do not move all runtime secrets into GitHub if the running app is managed by Coolify

## Compose rule

- The repo copy of `docker-compose.yaml` is authoritative
- If a service exists, it should be declared in the repo
- Do not assume server-side Compose changes are valid unless they are committed
- After changing `docker-compose.yaml`, the CI workflow automatically syncs it to Coolify's DB — do not manually update Coolify's compose through the UI

## Coolify-specific warnings

**Do not add Docker `HEALTHCHECK` instructions** to `Dockerfile` or `docker-compose.yaml` services that other services `depends_on`. Coolify silently converts `depends_on: service` to `depends_on: service: condition: service_healthy` whenever the dependency has a healthcheck. If the healthcheck fails during startup (even briefly), the dependent service never starts. The backend's `/health` endpoint exists for external monitoring only — do not wire it to a Docker healthcheck.

**Do not add `pull_policy: always` to `open-webui` or `novnc`**. These images do not change on our pushes. Always-pulling them forces a full container restart (90+ second outage) every deploy. Only `openmanus-backend` should have `pull_policy: always`.

## Change discipline

When making changes:

- prefer small, explicit edits
- preserve the single-branch workflow
- keep deployment logic simple
- avoid introducing tools or processes that require manual server babysitting

## Decision preference

When multiple valid options exist, prefer the option that:

- keeps `main` as the single source of truth
- keeps production behavior reproducible
- reduces hidden state on the server
- is easier for a non-developer owner to understand and audit

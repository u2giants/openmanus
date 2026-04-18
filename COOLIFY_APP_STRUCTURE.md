# Coolify App Structure

How the OpenManus stack is organized on the Coolify server, and how the deployment paths relate to each other.

---

## Directory Layout

```
/worksp/<app-name>/
├── app/                          # git clone of the source repo
│   ├── Dockerfile
│   ├── server.py
│   ├── entrypoint.sh
│   ├── config.toml
│   ├── novnc-startup.sh
│   ├── cdp_proxy.py
│   ├── docker-compose.yaml
│   └── ...
└── server -> /data/coolify/applications/<coolify-app-id>   # symlink

/data/coolify/
├── services/<uuid>/               # Coolify service deployment config
│   └── docker-compose.yml         # The compose file Coolify actually runs
├── applications/<uuid>/            # Coolify application source files
│   └── ...                        # Symlinked from /worksp/<app-name>/server
└── proxy/
    └── dynamic/                    # Traefik dynamic config (can cause stale routes)
```

---

## Key Paths Explained

### `/worksp/<app-name>/app/`

This is the **git clone** of the repository. It contains the source of truth for all code, Dockerfiles, and compose files. When you push to `main`, GitHub Actions builds from this repo and pushes the image to GHCR.

### `/worksp/<app-name>/server`

A **symlink** to `/data/coolify/applications/<coolify-app-id>`. This is Coolify's view of the application. Coolify reads configuration from this path.

### `/data/coolify/services/<uuid>/`

Coolify's **service deployment directory**. The `docker-compose.yml` that Coolify actually executes lives here. This is NOT the same as the `docker-compose.yaml` in the git repo — Coolify may modify it (e.g., adding labels, adjusting networks).

### `/data/coolify/applications/<uuid>/`

Coolify's **application source directory**. Linked from `/worksp/<app-name>/server`. Coolify uses this path for application-level configuration and metadata.

---

## How to Find the Coolify App ID

1. **Via the Coolify UI**: Navigate to the project → the application → look at the URL. It contains the UUID, e.g., `https://coolify.example.com/project/xxx/application/<uuid>`.

2. **Via the server filesystem**: Look at the symlink target:
   ```bash
   ls -la /worksp/<app-name>/server
   # Output: /worksp/<app-name>/server -> /data/coolify/applications/<uuid>
   ```
   The `<uuid>` at the end is the Coolify application ID.

3. **Via the Coolify API**: Use the deploy webhook URL — the `uuid` parameter is the application ID:
   ```
   http://<server>:8000/api/v1/deploy?uuid=<coolify-app-id>&force=true
   ```

---

## How Volume Mounts Work

The `docker-compose.yaml` in the git repo references files like `./novnc-startup.sh` and `./cdp_proxy.py` as bind mounts:

```yaml
volumes:
  - ./novnc-startup.sh:/custom-cont-init.d/99-start-chromium.sh:ro
  - ./cdp_proxy.py:/custom-cont-init.d/cdp_proxy.py:ro
```

**Important**: These relative paths resolve relative to the **Coolify service directory** (`/data/coolify/services/<uuid>/`), NOT the git repo directory. Coolify copies the files from the application path to the service path during deployment.

If the files are missing from the service directory, Docker will create **directories** at the mount points instead of files, causing the scripts to fail silently. See [TROUBLESHOOTING.md — Volume Mounts Created as Directories](TROUBLESHOOTING.md) for the fix.

### Verifying Mount Files Exist

```bash
# Check the service directory for the mounted files
ls -la /data/coolify/services/<uuid>/novnc-startup.sh
ls -la /data/coolify/services/<uuid>/cdp_proxy.py
```

If these are missing, the Coolify deployment may not have copied them. You can manually copy from the app directory:

```bash
cp /worksp/<app-name>/app/novnc-startup.sh /data/coolify/services/<uuid>/novnc-startup.sh
cp /worksp/<app-name>/app/cdp_proxy.py /data/coolify/services/<uuid>/cdp_proxy.py
```

Then redeploy via Coolify or `docker compose up -d` in the service directory.

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

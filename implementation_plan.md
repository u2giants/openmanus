# Implementation Plan: Fix OpenManus Backend Restart Loop

## Problem

The `openmanus-backend` container on Coolify is crash-looping every ~68 seconds with this error:

```
File "/app/main.py", line 20, in main
    prompt = args.prompt if args.prompt else input("Enter your prompt: ")
EOFError: EOF when reading a line
```

### Root Cause (CONFIRMED via container logs)

**The upstream OpenManus `main.py` is a CLI tool, not an HTTP server.** It calls `input("Enter your prompt: ")` which immediately fails with `EOFError` because Docker containers have no interactive stdin. The container starts, Python loads successfully, Daytona initializes correctly, then `input()` crashes, and `restart: unless-stopped` restarts it in an infinite loop.

### Deeper Architecture Problem

`open-webui` (the frontend at `https://manus.designflow.app`) is configured with:
```yaml
OPENAI_API_BASE_URL=http://openmanus-backend:8000/v1
```

This means open-webui expects an **OpenAI-compatible HTTP API** (specifically `/v1/chat/completions` and `/v1/models` endpoints) at port 8000. But OpenManus has **no HTTP server mode**. It only has:

- `main.py` — interactive CLI (crashes in Docker)
- `run_mcp_server.py` — MCP stdio server (not HTTP)
- `protocol/a2a/app/main.py` — A2A protocol server on port 10000 (not OpenAI-compatible)

**None of the suspected root causes from the ticket were correct:**

| Suspected Cause | Actual Status |
|---|---|
| ❌ Tailscale volume mount | Coolify's live compose has no volume mount (`Binds: null`) — not the issue |
| ✅ `python main.py` crashes | **YES** — `input()` → `EOFError` because no stdin in Docker |
| ❌ Port conflict | Port 8001:8000 mapping works fine, container starts |
| ❌ Missing env vars | `entrypoint.sh` IS running, env vars are substituted correctly |

### What's Already Available in the Image

- **FastAPI** 0.115.14 ✅ (already installed)
- **uvicorn** 0.34.3 ✅ (already installed)
- **openai** SDK ✅ (already installed)
- **Manus agent** class at `app.agent.manus.Manus` ✅
- **SandboxManus agent** at `app.agent.sandbox_agent.SandboxManus` ✅

## Solution

Create a **lightweight FastAPI server** (`server.py`) that:
1. Exposes OpenAI-compatible `/v1/chat/completions` and `/v1/models` endpoints
2. Accepts chat messages from open-webui
3. Delegates to the OpenManus `Manus` agent
4. Returns responses in OpenAI chat completion format
5. Runs on `0.0.0.0:8000` via uvicorn

Update `entrypoint.sh` to run `server.py` instead of `main.py`.

### Architecture

```
open-webui (port 3000)
    │
    │  POST /v1/chat/completions
    │  GET  /v1/models
    ▼
server.py (FastAPI, port 8000)   ← NEW FILE
    │
    │  await agent.run(prompt)
    ▼
app.agent.manus.Manus            ← existing OpenManus agent
    │
    │  LLM calls via config.toml
    ▼
OpenRouter API (openai/gpt-4o)
```

## Step-by-Step Tasks

### Task 1: Create `server.py` (NEW FILE — Builder)

Create `/app/server.py` (copied into image via Dockerfile) that implements:

```python
# Minimal OpenAI-compatible API wrapper around OpenManus Manus agent
# Endpoints:
#   GET  /v1/models          → list available models
#   POST /v1/chat/completions → run Manus agent with last user message
#   GET  /health              → health check
```

**Key design decisions for `server.py`:**
- Use `app.agent.manus.Manus` (not SandboxManus) — it's the standard agent
- Extract the last user message from the chat completion request as the prompt
- Run `agent.run(prompt)` and capture the result
- Return a properly formatted OpenAI chat completion response
- Create a fresh agent per request (agents are stateful, not thread-safe)
- Add a `/health` endpoint for monitoring
- Handle errors gracefully — return OpenAI-format error responses, don't crash the server
- Use `asyncio` properly — Manus agent is async

**Skeleton structure:**
```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
import asyncio
import uuid
import time

app = FastAPI(title="OpenManus API")

@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": "manus", "object": "model", ...}]}

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    # Extract last user message
    prompt = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    # Run Manus agent
    agent = await Manus.create()
    try:
        result = await agent.run(prompt)
    finally:
        await agent.cleanup()
    # Return OpenAI-format response
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:8]}", "object": "chat.completion", ...}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Task 2: Update `entrypoint.sh` (MODIFY)

Change from:
```sh
exec python main.py
```
To:
```sh
exec python server.py
```

### Task 3: Update `Dockerfile` (MODIFY)

Add `COPY server.py ./server.py` after the existing COPY lines:
```dockerfile
COPY config.toml ./config/config.toml
COPY entrypoint.sh ./entrypoint.sh
COPY server.py ./server.py          # ← ADD THIS LINE
```

### Task 4: Remove Tailscale volume mount from `docker-compose.yaml` (MODIFY)

Remove the `volumes` section from `openmanus-backend` service:
```yaml
# REMOVE:
    volumes:
      - /mnt/tailscale/souls:/app/custom_souls
```

The Tailscale path doesn't exist on the Coolify server (empty directory at `/mnt/tailscale/souls`). Coolify is already ignoring it in its generated compose, but we should clean up the repo to avoid confusion.

### Task 5: Verify port mapping in `docker-compose.yaml`

Keep `8000:8000` in the repo's compose. Coolify maps it to `8001:8000` in its generated compose, which is fine — `open-webui` connects via Docker network name (`openmanus-backend:8000`), not via host port.

### Task 6: Push, build, deploy (Reviewer-Pusher + DevOps)

1. Push changes to GitHub → triggers CI
2. CI builds new image with `server.py` baked in
3. Coolify deploys (or manual pull + recreate if webhook fails)
4. Verify backend starts and stays running

## Files Changed

| File | Action | Description |
|---|---|---|
| `server.py` | **CREATE** | FastAPI OpenAI-compatible API wrapper |
| `entrypoint.sh` | **MODIFY** | Change `exec python main.py` → `exec python server.py` |
| `Dockerfile` | **MODIFY** | Add `COPY server.py ./server.py` |
| `docker-compose.yaml` | **MODIFY** | Remove Tailscale volume mount |

## New Image Build Required?

**YES** — a new Docker image build + push is required. The fix involves:
1. A new file (`server.py`) that must be baked into the image
2. The entrypoint change (already in image via COPY)

A Coolify env var change alone is NOT sufficient.

## Validation Approach

1. **Startup validation**: Container logs should show uvicorn starting on `0.0.0.0:8000` (no `EOFError`)
2. **Health check**: `curl http://localhost:8001/health` from the Coolify server returns `{"status": "ok"}`
3. **Models endpoint**: `curl http://localhost:8001/v1/models` returns a model list
4. **Chat completion**: `curl -X POST http://localhost:8001/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"manus","messages":[{"role":"user","content":"What is 2+2?"}]}'` returns a response
5. **Container stability**: `docker ps` shows the backend as `Up` for >5 minutes (no restart)
6. **Frontend integration**: `https://manus.designflow.app` can send a message and get a response

## Rollback Plan

1. **If new image breaks**: Revert the GitHub commit, push, and redeploy. The old image (crash-looping) is no worse than current state.
2. **If server.py has import errors**: SSH into server, `docker exec` into container, run `python -c "from app.agent.manus import Manus"` to debug.
3. **If open-webui can't connect**: Check Docker network connectivity with `docker exec open-webui curl http://openmanus-backend:8000/health`.
4. **Nuclear option**: Stop `openmanus-backend` entirely — `open-webui` will still serve its UI, just without backend agent functionality.

## Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Manus agent import fails at server startup | Low | Agent already loads successfully in current crash loop |
| Agent.run() hangs or takes too long | Medium | Add timeout to agent.run() call (e.g., 300s) |
| Memory leak from creating agent per request | Medium | Monitor; future: add agent pooling |
| open-webui sends unexpected request format | Low | Log full request body, handle gracefully |
| Coolify webhook still returns 503 | High | Manual `docker pull` + recreate as fallback |

## Key Constraint

**Do NOT touch the `open-webui` service** — it's live at `https://manus.designflow.app` and working. All changes are isolated to the `openmanus-backend` service.

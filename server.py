"""
OpenAI-compatible HTTP API wrapper around OpenManus Manus agent.
Runs on 0.0.0.0:8000 via uvicorn.
"""
import uuid
import time
import json
import logging
import importlib.util
import sys
import inspect
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="OpenManus API")

# Directory for user-defined tools (persisted via Docker volume)
USER_TOOLS_DIR = Path("/app/user_tools")

# Patch the Manus system prompt at import time so the agent knows about the
# shared noVNC browser environment the user can see at vnc.designflow.app
try:
    import app.prompt.manus as manus_prompt
    manus_prompt.SYSTEM_PROMPT = (
        "You are OpenManus, an all-capable AI assistant, aimed at solving any task "
        "presented by the user. You have various tools at your disposal that you can "
        "call upon to efficiently complete complex requests.\n"
        "The initial directory is: {directory}\n\n"

        "IMPORTANT — Browser & noVNC setup:\n"
        "You have full control of a real Chromium browser running in a shared noVNC "
        "desktop. The user can see and interact with the same browser at "
        "https://vnc.designflow.app.\n\n"

        "RULES YOU MUST FOLLOW:\n"
        "1. Whenever you first use the browser on a task, immediately tell the user: "
        "'I have opened the browser. You can watch and interact with it at https://vnc.designflow.app'\n"
        "2. Before starting any long multi-step task that requires the user to be logged in "
        "(e.g. downloading account statements, filling forms), use the ask_human tool to "
        "confirm the user is already logged in. Navigate to the relevant page first so "
        "they can see it at https://vnc.designflow.app, then ask them to log in if needed "
        "and confirm before you proceed.\n"
        "3. Never claim you cannot open a browser or access a website. Always use "
        "your browser tools proactively.\n"
        "4. Never claim you cannot see the screen — you have browser tools to extract "
        "content and take screenshots.\n"
        "5. JAVASCRIPT SPA PAGES: Many financial sites (Fidelity, Schwab, Vanguard, etc.) "
        "are single-page applications. Raw HTML extraction will only show navigation — "
        "the actual content loads asynchronously. When a page shows mostly nav links:\n"
        "   a) Take a screenshot first to see what is visually rendered.\n"
        "   b) Use browser_evaluate to run JavaScript that waits for content: "
        "      e.g. document.querySelector('.your-selector')?.innerText or "
        "      Array.from(document.querySelectorAll('a, button')).map(e=>e.innerText).\n"
        "   c) Use browser_evaluate with a wait loop if needed: "
        "      await new Promise(r=>setTimeout(r,3000)) then re-extract.\n"
        "   d) Try clicking the visible element in the screenshot using click_element "
        "      by index rather than relying on extracted text.\n"
        "   e) Never give up after one HTML extraction attempt on a SPA.\n"
        "6. BULK FILE DOWNLOADS (e.g. account statements):\n"
        "   a) First take a screenshot to see all UI controls (account selector, date range, "
        "      statement type dropdowns).\n"
        "   b) Build a systematic plan: list all accounts, then for each account iterate "
        "      year by year, month by month.\n"
        "   c) Download each file by clicking its download link/button in the browser — "
        "      do NOT use python_execute to connect to the browser via Playwright or CDP.\n"
        "   d) After each download, use python_execute to verify the file landed in the "
        "      download directory (typically /root/Downloads or /config/chromium-profile/Downloads).\n"
        "   e) Keep a running log in /tmp/download_log.txt noting each file downloaded "
        "      or skipped, so progress is preserved if the session is interrupted.\n"
        "7. NEVER use python_execute to automate the browser (e.g. playwright.connect, "
        "   sync_playwright, Chrome DevTools Protocol). The browser is in a separate "
        "   container — Python code cannot reach it. Use only browser_use tool actions.\n"
        "8. When downloading files, use the browser to navigate and click download buttons "
        "   directly. Check screenshots to confirm what is actually rendered before clicking."
    )
    logger.info("Manus system prompt patched with noVNC browser info")
except Exception as e:
    logger.warning(f"Could not patch Manus system prompt: {e}")


# ---------------------------------------------------------------------------
# User tool loader
# ---------------------------------------------------------------------------

def load_user_tools() -> list:
    """
    Dynamically import BaseTool subclasses from /app/user_tools/*.py.
    Called on every agent request so edits take effect without restart.
    """
    tools = []
    if not USER_TOOLS_DIR.exists():
        return tools
    try:
        from app.tool.base import BaseTool
    except Exception as e:
        logger.warning(f"Cannot import BaseTool — skipping user tools: {e}")
        return tools

    for py_file in sorted(USER_TOOLS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"_user_tools.{py_file.stem}"
        # Remove cached version so saves take effect immediately
        sys.modules.pop(module_name, None)
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseTool)
                    and obj is not BaseTool
                    and obj.__module__ == module_name
                ):
                    tools.append(obj())
                    logger.info(f"Loaded user tool '{obj.name}' from {py_file.name}")
        except Exception as e:
            logger.warning(f"Failed to load user tool {py_file.name}: {e}")
    return tools


# ---------------------------------------------------------------------------
# Tool Manager UI — served at /admin/tools
# ---------------------------------------------------------------------------

TOOL_MANAGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OpenManus Tool Manager</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; background: #1e1e2e; color: #cdd6f4; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
    header { padding: 10px 16px; background: #181825; border-bottom: 1px solid #313244; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
    header h1 { font-size: 15px; font-weight: 600; }
    header span { color: #a6adc8; font-size: 12px; }
    .main { display: flex; flex: 1; overflow: hidden; }
    .sidebar { width: 200px; background: #181825; border-right: 1px solid #313244; display: flex; flex-direction: column; flex-shrink: 0; }
    .sidebar-header { padding: 8px; border-bottom: 1px solid #313244; }
    .new-btn { width: 100%; padding: 6px; background: #89b4fa; color: #1e1e2e; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; font-size: 13px; }
    .new-btn:hover { background: #74c7ec; }
    .tool-list { flex: 1; overflow-y: auto; }
    .tool-item { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #313244; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .tool-item:hover { background: #313244; }
    .tool-item.active { background: #45475a; color: #89b4fa; }
    .editor-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    .editor-toolbar { padding: 7px 10px; background: #181825; border-bottom: 1px solid #313244; display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
    .tool-name-input { padding: 5px 8px; background: #313244; border: 1px solid #45475a; border-radius: 4px; color: #cdd6f4; font-size: 13px; width: 180px; outline: none; }
    .tool-name-input:focus { border-color: #89b4fa; }
    .btn { padding: 5px 14px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 500; }
    .btn-save { background: #a6e3a1; color: #1e1e2e; }
    .btn-save:hover { background: #94e2d5; }
    .btn-delete { background: #f38ba8; color: #1e1e2e; }
    .btn-delete:hover { background: #eba0ac; }
    .btn:disabled { opacity: 0.4; cursor: default; }
    .cm-editor-wrap { flex: 1; overflow: hidden; }
    .CodeMirror { height: 100%; font-size: 13px; font-family: 'JetBrains Mono', 'Fira Code', monospace; }
    .status-bar { padding: 5px 12px; background: #181825; border-top: 1px solid #313244; font-size: 12px; color: #a6adc8; flex-shrink: 0; }
    .ok { color: #a6e3a1; }
    .err { color: #f38ba8; }
  </style>
</head>
<body>
  <header>
    <h1>OpenManus Tool Manager</h1>
    <span>Tools saved here are injected into every agent run automatically — no restart needed.</span>
  </header>
  <div class="main">
    <div class="sidebar">
      <div class="sidebar-header">
        <button class="new-btn" onclick="newTool()">+ New Tool</button>
      </div>
      <div class="tool-list" id="toolList"></div>
    </div>
    <div class="editor-panel">
      <div class="editor-toolbar">
        <input class="tool-name-input" id="toolName" placeholder="filename (no .py)" spellcheck="false" />
        <button class="btn btn-save" onclick="saveTool()">Save</button>
        <button class="btn btn-delete" id="deleteBtn" onclick="deleteTool()" disabled>Delete</button>
      </div>
      <div class="cm-editor-wrap" id="cmWrap">
        <textarea id="editor"></textarea>
      </div>
    </div>
  </div>
  <div class="status-bar" id="status">Select a tool from the list or create a new one.</div>

  <script>
    const TEMPLATE = `from app.tool.base import BaseTool, ToolResult


class MyCustomTool(BaseTool):
    name: str = "my_custom_tool"
    description: str = "Describe what this tool does so the AI knows when to use it."
    parameters: dict = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The input to process"
            }
        },
        "required": ["input"]
    }

    async def execute(self, input: str, **kwargs) -> ToolResult:
        # Your implementation here
        result = f"Processed: {input}"
        return ToolResult(output=result)
`;

    const cm = CodeMirror.fromTextArea(document.getElementById('editor'), {
      mode: 'python',
      theme: 'dracula',
      lineNumbers: true,
      indentUnit: 4,
      tabSize: 4,
      indentWithTabs: false,
      autofocus: true,
      extraKeys: { Tab: cm => cm.execCommand('indentMore') }
    });

    function resizeCM() {
      const wrap = document.getElementById('cmWrap');
      cm.setSize('100%', wrap.clientHeight + 'px');
    }
    window.addEventListener('resize', resizeCM);
    setTimeout(resizeCM, 50);

    async function loadTools() {
      const r = await fetch('/api/tools');
      const tools = await r.json();
      const list = document.getElementById('toolList');
      const active = document.getElementById('toolName').value;
      list.innerHTML = '';
      tools.forEach(t => {
        const div = document.createElement('div');
        div.className = 'tool-item' + (t === active ? ' active' : '');
        div.textContent = t;
        div.onclick = () => openTool(t);
        list.appendChild(div);
      });
    }

    async function openTool(name) {
      const r = await fetch('/api/tools/' + name);
      if (!r.ok) { setStatus('Could not load ' + name, 'err'); return; }
      const data = await r.json();
      document.getElementById('toolName').value = name;
      cm.setValue(data.code);
      document.getElementById('deleteBtn').disabled = false;
      document.querySelectorAll('.tool-item').forEach(el =>
        el.classList.toggle('active', el.textContent === name));
      setStatus('Loaded: ' + name + '.py', 'ok');
    }

    function newTool() {
      document.getElementById('toolName').value = '';
      cm.setValue(TEMPLATE);
      document.getElementById('deleteBtn').disabled = true;
      document.querySelectorAll('.tool-item').forEach(el => el.classList.remove('active'));
      setStatus('New tool — set a filename and save.', '');
      document.getElementById('toolName').focus();
    }

    async function saveTool() {
      const name = document.getElementById('toolName').value.trim();
      if (!name) { setStatus('Enter a filename first.', 'err'); return; }
      if (!/^[a-zA-Z0-9_]+$/.test(name)) {
        setStatus('Filename must be letters, digits, and underscores only.', 'err');
        return;
      }
      const code = cm.getValue();
      const r = await fetch('/api/tools/' + name, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
      });
      const data = await r.json();
      if (r.ok) {
        setStatus('Saved: ' + name + '.py', 'ok');
        document.getElementById('deleteBtn').disabled = false;
        await loadTools();
      } else {
        setStatus('Error: ' + (data.detail || JSON.stringify(data)), 'err');
      }
    }

    async function deleteTool() {
      const name = document.getElementById('toolName').value.trim();
      if (!name) return;
      if (!confirm('Delete ' + name + '.py?')) return;
      const r = await fetch('/api/tools/' + name, { method: 'DELETE' });
      if (r.ok) {
        newTool();
        await loadTools();
        setStatus('Deleted: ' + name + '.py', 'ok');
      } else {
        setStatus('Delete failed.', 'err');
      }
    }

    function setStatus(msg, type) {
      const el = document.getElementById('status');
      el.textContent = msg;
      el.className = 'status-bar' + (type ? ' ' + type : '');
    }

    document.getElementById('toolName').addEventListener('keydown', e => {
      if (e.key === 'Enter') saveTool();
    });

    loadTools();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Tool Manager API endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/tools", response_class=HTMLResponse)
async def tool_manager_ui():
    return TOOL_MANAGER_HTML


@app.get("/api/tools")
async def list_tools():
    USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(p.stem for p in USER_TOOLS_DIR.glob("*.py") if not p.name.startswith("_"))
    return names


@app.get("/api/tools/{name}")
async def get_tool(name: str):
    if not name.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid tool name")
    path = USER_TOOLS_DIR / f"{name}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Tool not found")
    return {"name": name, "code": path.read_text()}


@app.post("/api/tools/{name}")
async def save_tool(name: str, request: Request):
    if not name.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid tool name")
    body = await request.json()
    code = body.get("code", "")
    if not code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty")
    USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_TOOLS_DIR / f"{name}.py"
    path.write_text(code)
    return {"saved": True, "name": name}


@app.delete("/api/tools/{name}")
async def delete_tool(name: str):
    if not name.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid tool name")
    path = USER_TOOLS_DIR / f"{name}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Tool not found")
    path.unlink()
    return {"deleted": True, "name": name}


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    import os, httpx
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/models/user",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning(f"Failed to fetch OpenRouter models: {e}")
    return {
        "object": "list",
        "data": [
            {
                "id": "manus",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "openmanus",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
        messages = body.get("messages", [])
        stream = body.get("stream", False)
        requested_model = body.get("model")
        requested_temperature = body.get("temperature")
        requested_max_tokens = body.get("max_tokens")

        prompt = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "No user message found", "type": "invalid_request_error"}},
            )

        logger.info(
            f"Running Manus agent | model={requested_model} "
            f"stream={stream} temperature={requested_temperature} "
            f"max_tokens={requested_max_tokens} | prompt={prompt[:100]}..."
        )

        from app.agent.manus import Manus
        from app.llm import LLM
        from app.schema import Message

        llm = LLM()
        if requested_model and requested_model not in ("manus", "openmanus"):
            llm.model = requested_model
        if requested_temperature is not None:
            llm.temperature = float(requested_temperature)
        if requested_max_tokens is not None:
            llm.max_tokens = int(requested_max_tokens)

        prior_messages = []
        for msg in messages[:-1]:
            role = msg.get("role", "user")
            content = msg.get("content") or ""
            if role == "user":
                prior_messages.append(Message.user_message(content))
            elif role == "assistant":
                prior_messages.append(Message.assistant_message(content))
            elif role == "system":
                prior_messages.append(Message.system_message(content))

        agent = await Manus.create(max_steps=2000)
        if prior_messages:
            agent.messages = prior_messages

        # Inject any user-defined tools from /app/user_tools/
        user_tools = load_user_tools()
        for tool in user_tools:
            try:
                agent.available_tools.add_tool(tool)
            except Exception as e:
                logger.warning(f"Could not inject user tool '{getattr(tool, 'name', tool)}': {e}")

        try:
            result = await agent.run(prompt)
        finally:
            await agent.cleanup()

        if hasattr(result, "content"):
            answer = result.content
        elif isinstance(result, str):
            answer = result
        else:
            answer = str(result)

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model_id = requested_model or "manus"

        try:
            prompt_tokens = llm.count_tokens(" ".join(m.get("content", "") or "" for m in messages))
            completion_tokens = llm.count_tokens(answer or "")
        except Exception:
            prompt_tokens = len(prompt.split())
            completion_tokens = len(answer.split()) if answer else 0

        if stream:
            async def event_stream():
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"

                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"content": answer}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"

                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    except Exception as e:
        logger.error(f"Error in chat_completions: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "server_error"}},
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

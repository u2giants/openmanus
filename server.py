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
# OpenWebUI function: "Save to Knowledge" action
# Served at /admin/functions/save_to_knowledge.py so users can copy-paste it
# into OpenWebUI Admin → Functions.
# ---------------------------------------------------------------------------

SAVE_TO_KNOWLEDGE_FUNCTION = '''"""
OpenWebUI Action Function: Save to Knowledge
============================================
Install via OpenWebUI Admin → Functions → + (New Function).
Paste the entire contents of this file.

When a user clicks the "Save to Knowledge" button on an AI message, this action:
  1. Sends the full conversation to the AI with instructions to identify the
     current topic/issue and write a structured knowledge piece about it.
     (The AI decides what constitutes the relevant scope — no arbitrary cutoff.)
  2. Saves the result to the "OpenManus Knowledge" collection (auto-created).

Valves (Admin → Functions → gear icon):
  - knowledge_collection_name: collection to save into (default: "OpenManus Knowledge")
  - base_url: internal OpenWebUI URL (default: "http://localhost:8080")
"""

import time
import httpx
from pydantic import BaseModel, Field

# Hard cap: never send more than this many messages to the summariser.
# Protects against runaway token costs on very long sessions.
_MAX_MESSAGES = 50


class Action:

    class Valves(BaseModel):
        knowledge_collection_name: str = Field(
            default="OpenManus Knowledge",
            description="Name of the Knowledge collection to save pieces into. Created automatically if it doesn\'t exist.",
        )
        base_url: str = Field(
            default="http://localhost:8080",
            description="Internal base URL of the OpenWebUI instance (used for API calls).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _auth_headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get_or_create_knowledge(self, base_url: str, headers: dict, name: str):
        try:
            r = httpx.get(f"{base_url}/api/v1/knowledge/", headers=headers, timeout=10)
            r.raise_for_status()
            for col in r.json():
                if col.get("name") == name:
                    return col["id"]
            r2 = httpx.post(
                f"{base_url}/api/v1/knowledge/create",
                headers=headers,
                json={"name": name, "description": "Automatically saved knowledge pieces from OpenManus chats."},
                timeout=10,
            )
            r2.raise_for_status()
            return r2.json()["id"]
        except Exception:
            return None

    def _save_text_to_knowledge(self, base_url: str, headers: dict, knowledge_id: str, filename: str, content: str) -> bool:
        try:
            upload_headers = {k: v for k, v in headers.items() if k != "Content-Type"}
            r = httpx.post(
                f"{base_url}/api/v1/files/",
                headers=upload_headers,
                files={"file": (filename, content.encode("utf-8"), "text/plain")},
                timeout=30,
            )
            r.raise_for_status()
            file_id = r.json()["id"]
            r2 = httpx.post(
                f"{base_url}/api/v1/files/{file_id}/data/content/update",
                headers=headers,
                json={"content": content},
                timeout=30,
            )
            r2.raise_for_status()
            r3 = httpx.post(
                f"{base_url}/api/v1/knowledge/{knowledge_id}/file/add",
                headers=headers,
                json={"file_id": file_id},
                timeout=30,
            )
            r3.raise_for_status()
            return True
        except Exception:
            return False

    def _generate_knowledge_text(self, base_url: str, headers: dict, model: str, conversation: list) -> str:
        system_message = {
            "role": "system",
            "content": (
                "You are a knowledge base curator. You will be given a conversation that may "
                "cover multiple topics. Your job is to identify the main issue, task, or topic "
                "being worked on — it is usually the most recent coherent thread, though it may "
                "span the whole conversation. Ignore unrelated earlier exchanges.\\n\\n"
                "Write a concise, well-structured knowledge piece about that topic using this format:\\n\\n"
                "# [Descriptive Title]\\n\\n"
                "**Summary:** One or two sentences describing what this covers.\\n\\n"
                "## Key Points\\n"
                "- ...\\n\\n"
                "## Details\\n"
                "(Steps, commands, code snippets, decisions, or anything needed to reproduce or understand this.)\\n\\n"
                "Be specific and factual. Output only the knowledge piece — no preamble or explanation."
            ),
        }
        messages = [system_message] + conversation + [
            {"role": "user", "content": "Identify the main topic and write the knowledge piece now."}
        ]
        try:
            r = httpx.post(
                f"{base_url}/api/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages, "stream": False},
                timeout=120,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return ""

    async def action(self, body: dict, __user__: dict = None, __event_emitter__=None, __event_call__=None) -> None:
        async def emit(description: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": description, "done": done}})

        user_token = (__user__ or {}).get("token") or (__user__ or {}).get("id", "")
        model = body.get("model", "")
        all_messages = body.get("messages", [])

        # Full conversation, capped at _MAX_MESSAGES to limit token cost
        msgs = all_messages[-_MAX_MESSAGES:] if len(all_messages) > _MAX_MESSAGES else all_messages
        clean = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in msgs
            if m.get("role") in ("user", "assistant")
        ]

        if not clean:
            await emit("No conversation found to summarize.", done=True)
            return

        base_url = self.valves.base_url
        headers = self._auth_headers(user_token)

        await emit("Identifying topic and writing knowledge piece…")
        text = self._generate_knowledge_text(base_url, headers, model, clean)

        if not text or not text.strip():
            await emit("AI returned an empty response. Nothing saved.", done=True)
            return

        await emit("Saving to Knowledge base…")
        collection_name = self.valves.knowledge_collection_name
        knowledge_id = self._get_or_create_knowledge(base_url, headers, collection_name)

        if not knowledge_id:
            await emit(f"Could not access knowledge collection \'{collection_name}\'. Check base_url valve.", done=True)
            return

        first_line = text.strip().splitlines()[0].lstrip("#").strip()[:60]
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in first_line).strip()
        filename = f"{time.strftime(\'%Y%m%d_%H%M%S\')}_{safe_name or \'knowledge\'}.txt"

        ok = self._save_text_to_knowledge(base_url, headers, knowledge_id, filename, text)

        if ok:
            await emit(f"Saved to \'{collection_name}\' \u2192 \'{filename}\'", done=True)
            if __event_emitter__:
                await __event_emitter__({"type": "notification", "data": {"type": "success", "content": f"Saved to \'{collection_name}\'"}})
        else:
            await emit("Upload failed. Check that Knowledge API is enabled and base_url is correct.", done=True)
'''

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
    .auto-note { margin-left: auto; font-size: 11px; background: #313244; border-radius: 4px; padding: 3px 8px; color: #a6e3a1; }
    .fn-link { font-size: 11px; background: #45475a; border-radius: 4px; padding: 3px 8px; color: #cba6f7; text-decoration: none; white-space: nowrap; }
    .fn-link:hover { background: #585b70; }
    .main { display: flex; flex: 1; overflow: hidden; }
    /* Sidebar */
    .sidebar { width: 210px; background: #181825; border-right: 1px solid #313244; display: flex; flex-direction: column; flex-shrink: 0; }
    .sidebar-header { padding: 8px; border-bottom: 1px solid #313244; }
    .new-btn { width: 100%; padding: 6px; background: #89b4fa; color: #1e1e2e; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; font-size: 13px; }
    .new-btn:hover { background: #74c7ec; }
    .tool-list { flex: 1; overflow-y: auto; }
    .tool-item { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #313244; font-size: 13px; }
    .tool-item:hover { background: #313244; }
    .tool-item.active { background: #45475a; }
    .tool-item-filename { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .tool-item.active .tool-item-filename { color: #89b4fa; }
    .tool-item-meta { font-size: 11px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #a6adc8; }
    .tool-item-meta.ok  { color: #a6e3a1; }
    .tool-item-meta.err { color: #f38ba8; }
    .tool-item-meta.warn{ color: #f9e2af; }
    /* Editor panel */
    .editor-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    .editor-toolbar { padding: 7px 10px; background: #181825; border-bottom: 1px solid #313244; display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
    .tool-name-input { padding: 5px 8px; background: #313244; border: 1px solid #45475a; border-radius: 4px; color: #cdd6f4; font-size: 13px; width: 180px; outline: none; }
    .tool-name-input:focus { border-color: #89b4fa; }
    .btn { padding: 5px 14px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 500; }
    .btn-save   { background: #a6e3a1; color: #1e1e2e; }
    .btn-save:hover   { background: #94e2d5; }
    .btn-delete { background: #f38ba8; color: #1e1e2e; }
    .btn-delete:hover { background: #eba0ac; }
    .btn-upload { background: #cba6f7; color: #1e1e2e; }
    .btn-upload:hover { background: #b4befe; }
    .btn-run    { background: #fab387; color: #1e1e2e; }
    .btn-run:hover    { background: #f9e2af; }
    .btn:disabled { opacity: 0.4; cursor: default; }
    .cm-editor-wrap { flex: 1; overflow: hidden; min-height: 0; }
    .CodeMirror { height: 100%; font-size: 13px; font-family: 'JetBrains Mono', 'Fira Code', monospace; }
    /* Invoke panel */
    .invoke-panel { border-top: 2px solid #313244; background: #181825; flex-shrink: 0; display: none; flex-direction: column; }
    .invoke-panel.open { display: flex; }
    .invoke-header { padding: 6px 10px; display: flex; gap: 8px; align-items: center; background: #1e1e2e; border-bottom: 1px solid #313244; flex-shrink: 0; }
    .invoke-header span { font-size: 12px; font-weight: 600; color: #fab387; }
    .invoke-tool-select { background: #313244; border: 1px solid #45475a; border-radius: 4px; color: #cdd6f4; font-size: 12px; padding: 3px 6px; outline: none; }
    .invoke-body { display: flex; gap: 0; overflow: hidden; height: 180px; }
    .invoke-params-wrap { flex: 1; display: flex; flex-direction: column; border-right: 1px solid #313244; }
    .invoke-label { font-size: 10px; color: #585b70; padding: 4px 8px 2px; text-transform: uppercase; letter-spacing: .05em; flex-shrink: 0; }
    .invoke-params { flex: 1; background: #1e1e2e; color: #cdd6f4; border: none; outline: none; font-family: 'JetBrains Mono', monospace; font-size: 12px; padding: 6px 10px; resize: none; width: 100%; }
    .invoke-output-wrap { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    .invoke-output { flex: 1; padding: 6px 10px; font-family: 'JetBrains Mono', monospace; font-size: 12px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; color: #a6adc8; }
    .invoke-output.ok  { color: #a6e3a1; }
    .invoke-output.err { color: #f38ba8; }
    /* Status bar */
    .status-bar { padding: 5px 12px; background: #181825; border-top: 1px solid #313244; font-size: 12px; color: #a6adc8; flex-shrink: 0; }
    .ok  { color: #a6e3a1; }
    .err { color: #f38ba8; }
  </style>
</head>
<body>
  <header>
    <h1>OpenManus Tool Manager</h1>
    <span>Write or upload a <code>.py</code> file — saved tools are injected into every chat automatically.</span>
    <span class="auto-note">✓ auto-loaded in every chat</span>
    <a class="fn-link" href="/admin/functions/save_to_knowledge.py" target="_blank" title="Download and paste into OpenWebUI Admin → Functions to add a Save-to-Knowledge button on AI messages">⬇ Save-to-Knowledge function</a>
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
        <button class="btn btn-save"   onclick="saveTool()">Save</button>
        <button class="btn btn-upload" onclick="document.getElementById('fileUpload').click()">Upload .py</button>
        <input type="file" id="fileUpload" accept=".py" style="display:none" onchange="uploadFile(event)" />
        <button class="btn btn-delete" id="deleteBtn" onclick="deleteTool()" disabled>Delete</button>
      </div>
      <div class="cm-editor-wrap" id="cmWrap">
        <textarea id="editor"></textarea>
      </div>
      <!-- Invoke panel — shown when a valid tool is selected -->
      <div class="invoke-panel" id="invokePanel">
        <div class="invoke-header">
          <span>▶ Invoke</span>
          <select class="invoke-tool-select" id="invokeToolSelect" onchange="onToolSelect()"></select>
          <button class="btn btn-run" id="runBtn" onclick="runTool()">Run</button>
        </div>
        <div class="invoke-body">
          <div class="invoke-params-wrap">
            <div class="invoke-label">Parameters (JSON)</div>
            <textarea class="invoke-params" id="invokeParams" spellcheck="false" placeholder="{}"></textarea>
          </div>
          <div class="invoke-output-wrap">
            <div class="invoke-label">Output</div>
            <div class="invoke-output" id="invokeOutput">—</div>
          </div>
        </div>
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

    // All status data keyed by filename stem
    let statusMap = {};

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

    // Build sample JSON params from a parameters schema object
    function schemaToSample(schema) {
      if (!schema || schema.type !== 'object' || !schema.properties) return '{}';
      const sample = {};
      for (const [key, prop] of Object.entries(schema.properties)) {
        switch (prop.type) {
          case 'string':  sample[key] = ''; break;
          case 'number':
          case 'integer': sample[key] = 0; break;
          case 'boolean': sample[key] = false; break;
          case 'array':   sample[key] = []; break;
          case 'object':  sample[key] = {}; break;
          default:        sample[key] = null;
        }
      }
      return JSON.stringify(sample, null, 2);
    }

    async function loadTools() {
      const r = await fetch('/api/tools/status');
      const statuses = await r.json();
      statusMap = {};
      statuses.forEach(s => { statusMap[s.file] = s; });

      const list = document.getElementById('toolList');
      const active = document.getElementById('toolName').value;
      list.innerHTML = '';
      statuses.forEach(s => {
        const div = document.createElement('div');
        div.className = 'tool-item' + (s.file === active ? ' active' : '');
        div.onclick = () => openTool(s.file);

        const nameEl = document.createElement('div');
        nameEl.className = 'tool-item-filename';
        nameEl.textContent = s.file + '.py';
        div.appendChild(nameEl);

        const metaEl = document.createElement('div');
        if (s.error) {
          metaEl.className = 'tool-item-meta err';
          metaEl.textContent = '✗ ' + s.error.split('\\n').pop();
          div.title = s.error;
        } else if (s.tools.length > 0) {
          metaEl.className = 'tool-item-meta ok';
          metaEl.textContent = '✓ ' + s.tools.map(t => t.name).join(', ');
          div.title = s.tools.map(t => t.name + ': ' + t.description).join('\\n');
        } else {
          metaEl.className = 'tool-item-meta warn';
          metaEl.textContent = '⚠ no BaseTool subclass found';
          div.title = 'File loaded OK but no class extending BaseTool was detected. Check class definition and imports.';
        }
        div.appendChild(metaEl);
        list.appendChild(div);
      });

      // Refresh invoke panel for currently open tool
      updateInvokePanel(active);
    }

    function updateInvokePanel(filename) {
      const panel = document.getElementById('invokePanel');
      const sel   = document.getElementById('invokeToolSelect');
      const s = statusMap[filename];
      if (!s || !s.tools || s.tools.length === 0) {
        panel.classList.remove('open');
        setTimeout(resizeCM, 20);
        return;
      }
      // Populate tool selector
      const prev = sel.value;
      sel.innerHTML = '';
      s.tools.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t.name;
        opt.textContent = t.name;
        opt.dataset.params = schemaToSample(t.parameters);
        sel.appendChild(opt);
      });
      // Restore selection if still valid
      if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
      onToolSelect();
      panel.classList.add('open');
      setTimeout(resizeCM, 20);
    }

    function onToolSelect() {
      const sel = document.getElementById('invokeToolSelect');
      const opt = sel.options[sel.selectedIndex];
      if (opt) {
        document.getElementById('invokeParams').value = opt.dataset.params || '{}';
        document.getElementById('invokeOutput').textContent = '—';
        document.getElementById('invokeOutput').className = 'invoke-output';
      }
    }

    async function runTool() {
      const filename = document.getElementById('toolName').value.trim();
      const toolName = document.getElementById('invokeToolSelect').value;
      const paramsRaw = document.getElementById('invokeParams').value.trim();
      const outEl = document.getElementById('invokeOutput');
      const runBtn = document.getElementById('runBtn');

      let params;
      try { params = JSON.parse(paramsRaw || '{}'); }
      catch (e) { outEl.textContent = 'Invalid JSON: ' + e.message; outEl.className = 'invoke-output err'; return; }

      runBtn.disabled = true;
      outEl.textContent = 'Running…';
      outEl.className = 'invoke-output';

      try {
        const r = await fetch('/api/tools/' + filename + '/invoke', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tool: toolName, params })
        });
        const data = await r.json();
        if (data.error) {
          outEl.textContent = data.error;
          outEl.className = 'invoke-output err';
        } else {
          outEl.textContent = data.output;
          outEl.className = 'invoke-output ok';
        }
      } catch (e) {
        outEl.textContent = 'Request failed: ' + e.message;
        outEl.className = 'invoke-output err';
      } finally {
        runBtn.disabled = false;
      }
    }

    async function openTool(name) {
      const r = await fetch('/api/tools/' + name);
      if (!r.ok) { setStatus('Could not load ' + name, 'err'); return; }
      const data = await r.json();
      document.getElementById('toolName').value = name;
      cm.setValue(data.code);
      document.getElementById('deleteBtn').disabled = false;
      document.querySelectorAll('.tool-item').forEach(el =>
        el.classList.toggle('active', el.querySelector('.tool-item-filename')?.textContent === name + '.py'));
      setStatus('Loaded: ' + name + '.py', 'ok');
      updateInvokePanel(name);
    }

    function newTool() {
      document.getElementById('toolName').value = '';
      cm.setValue(TEMPLATE);
      document.getElementById('deleteBtn').disabled = true;
      document.querySelectorAll('.tool-item').forEach(el => el.classList.remove('active'));
      document.getElementById('invokePanel').classList.remove('open');
      setTimeout(resizeCM, 20);
      setStatus('New tool — set a filename and save.', '');
      document.getElementById('toolName').focus();
    }

    function uploadFile(event) {
      const file = event.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = e => {
        const name = file.name.replace(/\\.py$/, '');
        document.getElementById('toolName').value = name;
        cm.setValue(e.target.result);
        document.getElementById('deleteBtn').disabled = true;
        document.querySelectorAll('.tool-item').forEach(el => el.classList.remove('active'));
        setStatus('Loaded from file: ' + file.name + ' — click Save to store it.', 'ok');
      };
      reader.readAsText(file);
      event.target.value = '';
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


from fastapi.responses import PlainTextResponse

@app.get("/admin/functions/save_to_knowledge.py", response_class=PlainTextResponse)
async def get_save_to_knowledge_function():
    """Serve the Save-to-Knowledge OpenWebUI action function source."""
    return PlainTextResponse(SAVE_TO_KNOWLEDGE_FUNCTION, media_type="text/x-python")


@app.get("/api/tools")
async def list_tools():
    USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(p.stem for p in USER_TOOLS_DIR.glob("*.py") if not p.name.startswith("_"))
    return names


@app.get("/api/tools/status")
async def tools_status():
    """Load every user tool and return name, description, parameters schema, and any import error."""
    USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        from app.tool.base import BaseTool
        base_available = True
    except Exception:
        base_available = False

    for py_file in sorted(USER_TOOLS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        entry = {"file": py_file.stem, "tools": [], "error": None}
        if not base_available:
            entry["error"] = "OpenManus BaseTool not importable (check container)"
            results.append(entry)
            continue
        module_name = f"_user_tools_status.{py_file.stem}"
        sys.modules.pop(module_name, None)
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if (isinstance(obj, type) and issubclass(obj, BaseTool)
                        and obj is not BaseTool and obj.__module__ == module_name):
                    instance = obj()
                    entry["tools"].append({
                        "name": getattr(instance, "name", attr_name),
                        "description": getattr(instance, "description", ""),
                        "parameters": getattr(instance, "parameters", {}),
                    })
        except Exception as e:
            entry["error"] = str(e)
        results.append(entry)
    return results


@app.post("/api/tools/{name}/invoke")
async def invoke_tool(name: str, request: Request):
    """Instantiate a user tool and call execute() with the provided parameters."""
    if not name.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid tool name")
    path = USER_TOOLS_DIR / f"{name}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Tool not found")

    body = await request.json()
    tool_name = body.get("tool")   # which tool class (by name) to invoke
    params = body.get("params", {})

    try:
        from app.tool.base import BaseTool
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot import BaseTool: {e}")

    module_name = f"_user_tools_invoke.{name}"
    sys.modules.pop(module_name, None)
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import error: {e}")

    # Find the requested tool (or first tool if not specified)
    instance = None
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if (isinstance(obj, type) and issubclass(obj, BaseTool)
                and obj is not BaseTool and obj.__module__ == module_name):
            inst = obj()
            if tool_name is None or getattr(inst, "name", None) == tool_name:
                instance = inst
                break

    if instance is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found in {name}.py")

    try:
        result = await instance.execute(**params)
        return {"output": str(result.output) if hasattr(result, "output") else str(result)}
    except Exception as e:
        return {"error": str(e)}


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

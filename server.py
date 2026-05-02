"""
OpenAI-compatible HTTP API wrapper around OpenManus Manus agent.
Runs on 0.0.0.0:8000 via uvicorn.
"""

import asyncio
import contextlib
import os
import uuid
import time
import json
import logging
import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import subprocess as _subprocess

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import (
    JSONResponse,
    StreamingResponse,
    HTMLResponse,
    PlainTextResponse,
)
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="OpenManus API")

# Directory for user-defined tools (persisted via Docker volume)
USER_TOOLS_DIR = Path("/app/user_tools")
SETTINGS_FILE = USER_TOOLS_DIR / ".settings.json"

# Directory for downloaded files (statements, CSVs, etc.) — persisted via Docker volume
DOWNLOADS_DIR = Path("/app/downloads")
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _derive_ws_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path if parsed.path and parsed.path != "/" else "/ws"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


class ClawdTalkBridge:
    def __init__(self) -> None:
        self.api_key = os.environ.get("CLAWDTALK_API_KEY", "").strip()
        self.server_url = os.environ.get(
            "CLAWDTALK_SERVER_URL", "https://clawdtalk.com"
        ).rstrip("/")
        self.ws_url = (
            os.environ.get("CLAWDTALK_WS_URL", "") or _derive_ws_url(self.server_url)
        ).strip()
        self.agent_name = (
            os.environ.get("CLAWDTALK_AGENT_NAME", "OpenManus").strip() or "OpenManus"
        )
        self.owner_name = os.environ.get("CLAWDTALK_OWNER_NAME", "").strip()
        self.greeting = os.environ.get("CLAWDTALK_GREETING", "").strip()
        self.enabled = bool(self.api_key)
        self.available = False
        self.connected = False
        self.last_error = ""
        self.last_event_at = 0.0
        self.last_message_at = 0.0
        self.last_call_id = ""
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._conversations: dict[str, list[dict]] = {}
        self._active_calls: set[str] = set()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "connected": self.connected,
            "server_url": self.server_url,
            "ws_url": self.ws_url,
            "agent_name": self.agent_name,
            "owner_name": self.owner_name,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at or None,
            "last_message_at": self.last_message_at or None,
            "last_call_id": self.last_call_id or None,
            "active_calls": sorted(self._active_calls),
        }

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
            "User-Agent": "openmanus-clawdtalk/1.0",
        }

    async def start(self) -> None:
        if not self.enabled:
            logger.info("ClawdTalk bridge disabled: no CLAWDTALK_API_KEY configured")
            return
        try:
            import websockets  # noqa: F401
        except Exception as e:
            self.last_error = f"websockets dependency unavailable: {e}"
            logger.warning("ClawdTalk bridge unavailable: %s", self.last_error)
            return
        self.available = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info(
            "ClawdTalk bridge enabled | server=%s ws=%s", self.server_url, self.ws_url
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False
        self._active_calls.clear()

    async def _run_forever(self) -> None:
        import websockets

        backoff = 5
        while True:
            try:
                logger.info("Connecting to ClawdTalk websocket: %s", self.ws_url)
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=self._auth_headers(),
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=20,
                    max_size=2_000_000,
                ) as websocket:
                    self.connected = True
                    self.last_error = ""
                    backoff = 5
                    await self._handle_socket(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                logger.warning(
                    "ClawdTalk websocket error (retry in %ds): %s", backoff, e
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _handle_socket(self, websocket) -> None:
        async for raw in websocket:
            self.last_event_at = time.time()
            try:
                payload = json.loads(raw)
            except Exception:
                logger.debug("Ignoring non-JSON ClawdTalk frame: %r", raw)
                continue

            event = payload.get("event") or payload.get("type")
            if event == "message" and payload.get("call_id") and payload.get("text"):
                call_id = str(payload["call_id"])
                self.last_call_id = call_id
                self.last_message_at = time.time()
                self._active_calls.add(call_id)
                asyncio.create_task(self._handle_message(websocket, payload))
                continue

            if event in {"call_ended", "ended", "hangup"} and payload.get("call_id"):
                call_id = str(payload["call_id"])
                self._active_calls.discard(call_id)
                self._conversations.pop(call_id, None)
                continue

            if event in {"ping", "heartbeat"} and payload.get("call_id"):
                await self._send_json(
                    websocket, {"type": "pong", "call_id": payload["call_id"]}
                )

    async def _handle_message(self, websocket, payload: dict) -> None:
        call_id = str(payload["call_id"])
        text = str(payload.get("text", "")).strip()
        if not text:
            return

        history = self._conversations.setdefault(call_id, [])
        history.append({"role": "user", "content": text})
        history[:] = history[-20:]

        messages = history
        if len(history) == 1 and self.greeting:
            messages = [
                {
                    "role": "system",
                    "content": (
                        f"You are speaking over a live ClawdTalk phone call as {self.agent_name}. "
                        f"The human caller is {self.owner_name or 'the user'}. "
                        f"Keep answers concise and natural for voice. Opening greeting: {self.greeting}"
                    ),
                },
                *history,
            ]

        try:
            answer = await run_manus_agent(
                messages=messages,
                requested_model=None,
                requested_temperature=None,
                requested_max_tokens=None,
            )
        except Exception as e:
            logger.error("ClawdTalk call %s failed: %s", call_id, e, exc_info=True)
            answer = "I hit an internal error while processing that. Please try again."

        history.append({"role": "assistant", "content": answer})
        history[:] = history[-20:]
        await self._send_json(
            websocket, {"type": "response", "call_id": call_id, "text": answer}
        )

    async def _send_json(self, websocket, payload: dict) -> None:
        async with self._send_lock:
            await websocket.send(json.dumps(payload))

    async def create_call(self, payload: dict) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{self.server_url}/v1/calls",
                headers=self._auth_headers(),
                json=payload,
            )
        try:
            data = resp.json()
        except Exception:
            data = {"status_code": resp.status_code, "text": resp.text}
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=data)
        return data


clawdtalk_bridge = ClawdTalkBridge()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def get_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def write_settings(s: dict) -> None:
    USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


# ---------------------------------------------------------------------------
# OpenWebUI sync helpers
# ---------------------------------------------------------------------------


def _py_sig(props: dict, required: list) -> tuple:
    """Return (signature_parts, param_doc_lines, param_dict_str) for a tool."""
    type_map = {
        "string": ("str", '""'),
        "integer": ("int", "0"),
        "number": ("float", "0.0"),
        "boolean": ("bool", "False"),
        "array": ("list", "[]"),
        "object": ("dict", "{}"),
    }
    sig, docs = [], []
    for pname, pinfo in props.items():
        py_t, default = type_map.get(pinfo.get("type", "string"), ("str", '""'))
        sig.append(
            f"{pname}: {py_t}" if pname in required else f"{pname}: {py_t} = {default}"
        )
        docs.append(f"        :param {pname}: {pinfo.get('description', '')}")
    param_dict = ", ".join(f'"{k}": {k}' for k in props)
    return sig, docs, param_dict


def generate_owui_tool_code(file_stem: str, tools_info: list) -> str:
    first_desc = (
        tools_info[0].get("description", file_stem) if tools_info else file_stem
    )
    methods = []
    for tool in tools_info:
        name = tool["name"]
        desc = tool.get("description", "")
        params = tool.get("parameters", {})
        sig, docs, param_dict = _py_sig(
            params.get("properties", {}), params.get("required", [])
        )
        sig_str = ", ".join(sig)
        docs_str = "\n".join(docs) if docs else "        :return: tool output"
        methods.append(
            f"    def {name}(self, {sig_str}) -> str:\n"
            f'        """\n        {desc}\n{docs_str}\n        :return: tool output\n        """\n'
            f"        import httpx\n"
            f"        try:\n"
            f"            r = httpx.post(\n"
            f'                "http://openmanus-backend:8000/api/tools/{file_stem}/invoke",\n'
            f'                json={{"tool": "{name}", "params": {{{{{param_dict}}}}}}}, timeout=60)\n'
            f"            d = r.json()\n"
            f'            return str(d.get("error") or d.get("output", "(no output)"))\n'
            f"        except Exception as e:\n"
            f'            return f"Error: {{e}}"\n'
        )
    return (
        f'"""\n{first_desc}\nAuto-synced from OpenManus Tool Manager.\n"""\n\n\n'
        f"class Tools:\n    def __init__(self): pass\n\n" + "\n".join(methods)
    )


def _owui_tool_id(stem: str) -> str:
    return f"openmanus__{stem}"


def _session_token(request: Request) -> str:
    return request.cookies.get("token", "")


async def _owui_sync_one(stem: str, owui_url: str, token: str) -> tuple:
    import httpx as _hx

    try:
        from app.tool.base import BaseTool as _BT
    except Exception as e:
        return False, f"Cannot import BaseTool: {e}"
    mod_name = f"_owui_sync.{stem}"
    sys.modules.pop(mod_name, None)
    py_file = USER_TOOLS_DIR / f"{stem}.py"
    if not py_file.exists():
        return False, "File not found"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, py_file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        tools_info = [
            {
                "name": getattr(obj(), "name", obj.__name__),
                "description": getattr(obj(), "description", ""),
                "parameters": getattr(obj(), "parameters", {}),
            }
            for attr in dir(mod)
            for obj in [getattr(mod, attr)]
            if isinstance(obj, type)
            and issubclass(obj, _BT)
            and obj is not _BT
            and obj.__module__ == mod_name
        ]
    except Exception as e:
        return False, f"Import error: {e}"
    if not tools_info:
        return False, "No BaseTool subclass found"
    code = generate_owui_tool_code(stem, tools_info)
    tool_id = _owui_tool_id(stem)
    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with _hx.AsyncClient(timeout=15) as c:
        existing = await c.get(f"{owui_url}/api/v1/tools/id/{tool_id}", headers=hdrs)
        exists = existing.status_code == 200
        payload = {
            "id": tool_id,
            "name": (tools_info[0]["description"] or stem)[:50],
            "content": code,
            "meta": {"description": tools_info[0]["description"], "manifest": {}},
        }
        url = (
            f"{owui_url}/api/v1/tools/id/{tool_id}/update"
            if exists
            else f"{owui_url}/api/v1/tools/create"
        )
        r = await c.post(url, headers=hdrs, json=payload)
        if r.status_code < 300:
            return True, "Synced"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"


async def _owui_delete_one(stem: str, owui_url: str, token: str) -> tuple:
    import httpx as _hx

    hdrs = {"Authorization": f"Bearer {token}"}
    async with _hx.AsyncClient(timeout=10) as c:
        r = await c.delete(
            f"{owui_url}/api/v1/tools/id/{_owui_tool_id(stem)}/delete", headers=hdrs
        )
        return r.status_code in (200, 204, 404), f"HTTP {r.status_code}"


async def _owui_install_fn(
    fn_id: str, fn_name: str, code: str, owui_url: str, token: str
) -> tuple:
    import httpx as _hx

    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "id": fn_id,
        "name": fn_name,
        "content": code,
        "meta": {"description": fn_name, "manifest": {}},
    }
    async with _hx.AsyncClient(timeout=15) as c:
        exists = (
            await c.get(f"{owui_url}/api/v1/functions/id/{fn_id}", headers=hdrs)
        ).status_code == 200
        url = (
            f"{owui_url}/api/v1/functions/id/{fn_id}/update"
            if exists
            else f"{owui_url}/api/v1/functions/create"
        )
        r = await c.post(url, headers=hdrs, json=payload)
        if r.status_code < 300:
            return True, "Installed"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"


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
        "   directly. Check screenshots to confirm what is actually rendered before clicking.\n\n"
        "ERROR HANDLING — MANDATORY:\n"
        "9. When any tool call returns an error, immediately output the full error text to "
        "   the user verbatim, prefixed with '⚠️ Tool error:'. Do not silently swallow errors.\n"
        "10. If the same tool fails 2 or more times in a row with the same or similar error, "
        "    STOP immediately. Tell the user exactly which tool failed, show the exact error "
        "    message, explain what you were trying to do, and ask how they want to proceed. "
        "    Do not keep retrying indefinitely.\n"
        "11. After every 5 steps on any multi-step task, output a one-sentence progress "
        "    summary: what has been done so far and what remains.\n"
        "12. If you are about to give up or cannot make progress, tell the user explicitly "
        "    what blocked you (exact error or obstacle) rather than just saying the task "
        "    cannot be completed."
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


async def run_manus_agent(
    messages: list[dict],
    requested_model: str | None,
    requested_temperature: float | None,
    requested_max_tokens: int | None,
    selected_tool_names: set[str] | None = None,
) -> str:
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

    user_prompt = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    if not user_prompt:
        raise ValueError("No user message found")

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

    all_user_tools = load_user_tools()
    user_tools = (
        [t for t in all_user_tools if t.name in selected_tool_names]
        if selected_tool_names
        else all_user_tools
    )
    for tool in user_tools:
        try:
            agent.available_tools.add_tool(tool)
        except Exception as e:
            logger.warning(
                f"Could not inject user tool '{getattr(tool, 'name', tool)}': {e}"
            )

    try:
        result = await agent.run(user_prompt)
    finally:
        await agent.cleanup()

    if hasattr(result, "content"):
        return result.content
    if isinstance(result, str):
        return result
    return str(result)


# ---------------------------------------------------------------------------
# OpenWebUI function: "Run Tool" action
# Served at /admin/functions/run_tool.py
# ---------------------------------------------------------------------------

RUN_TOOL_FUNCTION = '''"""
OpenWebUI Action Function: Run Tool
=====================================
Install via OpenWebUI Admin → Functions → + (New Function).
Paste this entire file.

Adds a "Run Tool" button to AI messages. When clicked:
  1. Fetches your valid tools from the OpenManus backend.
  2. Shows a numbered picker dialog — type the number or tool name.
  3. Shows a parameters dialog pre-filled with the tool\'s JSON schema.
  4. Calls the tool directly and posts output back into the chat.

Valve:
  - backend_url: internal URL of the OpenManus backend
                 (default: "http://openmanus-backend:8000")
"""

import json
import httpx
from pydantic import BaseModel, Field


class Action:

    class Valves(BaseModel):
        backend_url: str = Field(
            default="http://openmanus-backend:8000",
            description="Internal URL of the OpenManus backend (used for tool list and invocation).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _get_valid_tools(self, backend_url: str) -> list:
        """Return a flat list of {file, name, description, parameters} for every valid tool."""
        try:
            r = httpx.get(f"{backend_url}/api/tools/status", timeout=10)
            r.raise_for_status()
            results = []
            for entry in r.json():
                if not entry.get("error") and entry.get("tools"):
                    for t in entry["tools"]:
                        results.append({
                            "file": entry["file"],
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {}),
                        })
            return results
        except Exception:
            return []

    def _schema_to_sample(self, schema: dict) -> str:
        """Build a sample JSON object from a JSON Schema parameters dict."""
        if not schema or schema.get("type") != "object" or not schema.get("properties"):
            return "{}"
        sample = {}
        for key, prop in schema["properties"].items():
            t = prop.get("type", "string")
            if t == "string":             sample[key] = ""
            elif t in ("number","integer"): sample[key] = 0
            elif t == "boolean":          sample[key] = False
            elif t == "array":            sample[key] = []
            else:                         sample[key] = None
        return json.dumps(sample, indent=2)

    async def action(
        self,
        body: dict,
        __user__: dict = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> None:

        async def emit(description: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": description, "done": done}}
                )

        if not __event_call__:
            await emit("__event_call__ not available in this OpenWebUI version.", done=True)
            return

        # ------------------------------------------------------------------ #
        # 1. Fetch tool list                                                  #
        # ------------------------------------------------------------------ #
        await emit("Loading tools…")
        backend_url = self.valves.backend_url
        tools = self._get_valid_tools(backend_url)

        if not tools:
            await emit("No valid tools found. Check the Tool Manager.", done=True)
            return

        # ------------------------------------------------------------------ #
        # 2. Show picker                                                       #
        # ------------------------------------------------------------------ #
        tool_list_text = "\\n".join(
            f"{i + 1}. {t[\'name\']} — {t[\'description\'][:80]}"
            for i, t in enumerate(tools)
        )

        pick_response = await __event_call__({
            "type": "input",
            "data": {
                "title": "Run Tool",
                "message": f"Available tools:\\n\\n{tool_list_text}\\n\\nEnter number or name:",
                "placeholder": "1",
            },
        })

        pick = str(pick_response or "").strip()
        if not pick:
            await emit("Cancelled.", done=True)
            return

        # Resolve by number or name
        selected = None
        if pick.isdigit():
            idx = int(pick) - 1
            if 0 <= idx < len(tools):
                selected = tools[idx]
        else:
            for t in tools:
                if t["name"] == pick:
                    selected = t
                    break

        if not selected:
            await emit(f"Tool \'{pick}\' not found. Enter the number shown in the list.", done=True)
            return

        # ------------------------------------------------------------------ #
        # 3. Parameters dialog                                                 #
        # ------------------------------------------------------------------ #
        sample = self._schema_to_sample(selected["parameters"])

        params_response = await __event_call__({
            "type": "input",
            "data": {
                "title": f"Parameters — {selected[\'name\']}",
                "message": f"{selected[\'description\']}\\n\\nEdit the JSON parameters below:",
                "placeholder": sample,
                "value": sample,
            },
        })

        params_raw = str(params_response or "{}").strip() or "{}"
        try:
            params = json.loads(params_raw)
        except json.JSONDecodeError as e:
            await emit(f"Invalid JSON: {e}", done=True)
            return

        # ------------------------------------------------------------------ #
        # 4. Invoke                                                            #
        # ------------------------------------------------------------------ #
        await emit(f"Running \'{selected[\'name\']}\'…")
        try:
            r = httpx.post(
                f"{backend_url}/api/tools/{selected[\'file\']}/invoke",
                json={"tool": selected["name"], "params": params},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            await emit(f"Request failed: {e}", done=True)
            return

        if "error" in data and data["error"]:
            await emit(f"Tool error: {data[\'error\']}", done=True)
            if __event_emitter__:
                await __event_emitter__({
                    "type": "message",
                    "data": {"content": f"\\n\\n**Tool error — `{selected[\'name\']}`:**\\n```\\n{data[\'error\']}\\n```"},
                })
            return

        output = data.get("output", "(no output)")
        await emit(f"Done — {selected[\'name\']}", done=True)

        if __event_emitter__:
            await __event_emitter__({
                "type": "message",
                "data": {"content": f"\\n\\n**Tool output — `{selected[\'name\']}`:**\\n```\\n{output}\\n```"},
            })
'''

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
    /* Header */
    header { padding: 8px 14px; background: #181825; border-bottom: 1px solid #313244; display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
    header h1 { font-size: 15px; font-weight: 600; white-space: nowrap; }
    .owui-badge { font-size: 11px; border-radius: 4px; padding: 3px 8px; white-space: nowrap; }
    .owui-connected { background: #1e3a2f; color: #a6e3a1; border: 1px solid #2d5a3d; }
    .owui-disconnected { background: #3a1e1e; color: #f38ba8; border: 1px solid #5a2d2d; }
    .owui-unknown { background: #313244; color: #a6adc8; border: 1px solid #45475a; }
    .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
    .hdr-btn { font-size: 11px; background: #313244; border: 1px solid #45475a; border-radius: 4px; padding: 4px 10px; color: #cdd6f4; cursor: pointer; white-space: nowrap; }
    .hdr-btn:hover { background: #45475a; }
    /* Settings panel */
    .settings-panel { background: #12121c; border-bottom: 2px solid #313244; flex-shrink: 0; padding: 14px 18px; display: none; }
    .settings-panel.open { display: block; }
    .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .settings-section-title { font-size: 12px; font-weight: 600; color: #89b4fa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: .05em; }
    .settings-field { margin-bottom: 8px; }
    .settings-field label { display: block; font-size: 11px; color: #a6adc8; margin-bottom: 4px; }
    .settings-input { width: 100%; padding: 5px 8px; background: #1e1e2e; border: 1px solid #45475a; border-radius: 4px; color: #cdd6f4; font-size: 12px; outline: none; }
    .settings-input:focus { border-color: #89b4fa; }
    .settings-row-btns { display: flex; gap: 8px; margin-top: 8px; }
    .settings-msg { font-size: 11px; margin-top: 6px; min-height: 16px; }
    .settings-msg.ok  { color: #a6e3a1; }
    .settings-msg.err { color: #f38ba8; }
    .fn-install-list { display: flex; flex-direction: column; gap: 6px; }
    .fn-install-row { display: flex; align-items: center; gap: 8px; }
    .fn-install-name { font-size: 12px; flex: 1; color: #cdd6f4; }
    .fn-status { font-size: 11px; color: #a6adc8; }
    /* Sidebar */
    .main { display: flex; flex: 1; overflow: hidden; }
    .sidebar { width: 210px; background: #181825; border-right: 1px solid #313244; display: flex; flex-direction: column; flex-shrink: 0; }
    .sidebar-header { padding: 8px; border-bottom: 1px solid #313244; }
    .new-btn { width: 100%; padding: 6px; background: #89b4fa; color: #1e1e2e; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; font-size: 13px; }
    .new-btn:hover { background: #74c7ec; }
    .tool-list { flex: 1; overflow-y: auto; }
    .tool-item { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #313244; font-size: 13px; }
    .tool-item:hover { background: #313244; }
    .tool-item.active { background: #45475a; }
    .tool-item-row { display: flex; align-items: baseline; gap: 4px; }
    .tool-item-filename { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
    .tool-item.active .tool-item-filename { color: #89b4fa; }
    .sync-icon { font-size: 10px; flex-shrink: 0; }
    .sync-icon.synced { color: #a6e3a1; }
    .sync-icon.unsynced { color: #585b70; }
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
    .btn-install { background: #45475a; color: #cdd6f4; font-size: 12px; padding: 4px 10px; }
    .btn-install:hover { background: #585b70; }
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
    <span id="owuiBadge" class="owui-badge owui-unknown">◌ Checking…</span>
    <div class="header-right">
      <button class="hdr-btn" onclick="toggleSettings()">⚙ Settings</button>
      <button class="hdr-btn" id="syncAllBtn" onclick="syncAllTools()" style="display:none">↻ Sync All</button>
    </div>
  </header>

  <div class="settings-panel" id="settingsPanel">
    <div class="settings-grid">
      <div>
        <div class="settings-section-title">OpenWebUI Connection</div>
        <p style="font-size:11px;color:#a6adc8;margin-bottom:10px">Auto-connect uses your existing login session — no API key needed.</p>
        <div class="settings-row-btns">
          <button class="btn btn-save" style="background:#89b4fa" onclick="autoConnect()">⚡ Auto-connect</button>
          <button class="btn" style="background:#74c7ec;color:#1e1e2e" onclick="testConnection()">Test</button>
        </div>
        <div class="settings-msg" id="settingsMsg"></div>
        <details style="margin-top:10px">
          <summary style="font-size:11px;color:#585b70;cursor:pointer">Advanced: change OpenWebUI URL</summary>
          <div style="margin-top:8px">
            <div class="settings-field">
              <label>OpenWebUI URL (internal)</label>
              <input id="owuiUrl" class="settings-input" type="text" placeholder="http://open-webui:8080" />
            </div>
            <div class="settings-row-btns">
              <button class="btn btn-save" onclick="saveSettings()">Save URL</button>
            </div>
          </div>
        </details>
      </div>
      <div>
        <div class="settings-section-title">Install Functions into OpenWebUI</div>
        <p style="font-size:11px;color:#a6adc8;margin-bottom:10px">One-click install — no copy-paste needed. Adds action buttons to AI messages in chat.</p>
        <div class="fn-install-list">
          <div class="fn-install-row">
            <span class="fn-install-name">▶ Run Tool — manually invoke any tool from a chat message</span>
            <button class="btn btn-install" id="installRunTool" onclick="installFunction('run_tool')">Install</button>
            <span class="fn-status" id="statusRunTool"></span>
          </div>
          <div class="fn-install-row">
            <span class="fn-install-name">💾 Save to Knowledge — save a chat summary to the knowledge base</span>
            <button class="btn btn-install" id="installSaveKnowledge" onclick="installFunction('save_to_knowledge')">Install</button>
            <span class="fn-status" id="statusSaveKnowledge"></span>
          </div>
        </div>
        <div class="settings-msg" id="installMsg"></div>
      </div>
    </div>
  </div>
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
    let owuiConnected = false;
    let syncedTools = new Set();

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

        const rowEl = document.createElement('div');
        rowEl.className = 'tool-item-row';

        const nameEl = document.createElement('div');
        nameEl.className = 'tool-item-filename';
        nameEl.textContent = s.file + '.py';
        rowEl.appendChild(nameEl);

        if (owuiConnected) {
          const syncEl = document.createElement('span');
          const isSynced = syncedTools.has(s.file);
          syncEl.className = 'sync-icon ' + (isSynced ? 'synced' : 'unsynced');
          syncEl.title = isSynced ? 'Synced to OpenWebUI' : 'Not yet synced to OpenWebUI';
          syncEl.textContent = isSynced ? '☁' : '○';
          rowEl.appendChild(syncEl);
        }
        div.appendChild(rowEl);

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
          div.title = 'File loaded OK but no class extending BaseTool was detected.';
        }
        div.appendChild(metaEl);
        list.appendChild(div);
      });

      updateInvokePanel(active);
    }

    async function loadSettings() {
      try {
        const r = await fetch('/api/owui/settings');
        const s = await r.json();
        document.getElementById('owuiUrl').value = s.owui_url || 'http://open-webui:8080';
        owuiConnected = s.connected || false;
        syncedTools = new Set(s.synced_tools || []);
        updateOwuiBadge();
      } catch(e) {
        console.error('loadSettings:', e);
      }
    }

    function updateOwuiBadge() {
      const badge = document.getElementById('owuiBadge');
      const syncBtn = document.getElementById('syncAllBtn');
      if (owuiConnected) {
        badge.className = 'owui-badge owui-connected';
        badge.textContent = '☁ Connected to OpenWebUI — select tools via ◈ in chat (use Manus Agent model)';
        syncBtn.style.display = '';
      } else {
        badge.className = 'owui-badge owui-disconnected';
        badge.textContent = '⚠ Not connected — all tools auto-loaded in every chat';
        syncBtn.style.display = 'none';
      }
    }

    function toggleSettings() {
      const p = document.getElementById('settingsPanel');
      p.classList.toggle('open');
    }

    async function autoConnect() {
      const msg = document.getElementById('settingsMsg');
      msg.textContent = 'Connecting…'; msg.className = 'settings-msg';
      try {
        const r = await fetch('/api/owui/bootstrap', { method: 'POST' });
        const d = await r.json();
        msg.textContent = d.ok ? ('✓ ' + d.message) : ('✗ ' + d.message);
        msg.className = 'settings-msg ' + (d.ok ? 'ok' : 'err');
        if (d.ok) {
          await loadSettings();
          await syncAllTools();
        }
      } catch(e) { msg.textContent = '✗ ' + e.message; msg.className = 'settings-msg err'; }
    }

    async function saveSettings() {
      const url = document.getElementById('owuiUrl').value.trim();
      const msg = document.getElementById('settingsMsg');
      msg.textContent = 'Saving…'; msg.className = 'settings-msg';
      try {
        const r = await fetch('/api/owui/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ owui_url: url }) });
        if (r.ok) {
          msg.textContent = '✓ Saved'; msg.className = 'settings-msg ok';
          await loadSettings();
          await loadTools();
        } else {
          msg.textContent = '✗ Save failed'; msg.className = 'settings-msg err';
        }
      } catch(e) { msg.textContent = '✗ ' + e.message; msg.className = 'settings-msg err'; }
    }

    async function testConnection() {
      const url = document.getElementById('owuiUrl').value.trim();
      const msg = document.getElementById('settingsMsg');
      msg.textContent = 'Testing…'; msg.className = 'settings-msg';
      try {
        const r = await fetch('/api/owui/test', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ owui_url: url }) });
        const d = await r.json();
        msg.textContent = d.ok ? ('✓ ' + d.message) : ('✗ ' + d.message);
        msg.className = 'settings-msg ' + (d.ok ? 'ok' : 'err');
        if (d.ok) { owuiConnected = true; updateOwuiBadge(); }
      } catch(e) { msg.textContent = '✗ ' + e.message; msg.className = 'settings-msg err'; }
    }

    async function syncAllTools() {
      const badge = document.getElementById('owuiBadge');
      badge.textContent = '↻ Syncing…';
      try {
        const r = await fetch('/api/owui/sync-all', { method: 'POST' });
        const d = await r.json();
        const results = d.results || {};
        const entries = Object.entries(results);
        const ok = entries.filter(([,v]) => v.ok).length;
        const failures = entries.filter(([,v]) => !v.ok);
        let msg = `Synced ${ok} tool(s) to OpenWebUI`;
        if (failures.length) {
          msg += ' — ' + failures.map(([name, v]) => `${name}: ${v.message}`).join('; ');
        }
        setStatus(msg, failures.length ? 'err' : 'ok');
        await loadSettings();
        await loadTools();
      } catch(e) { setStatus('Sync failed: ' + e.message, 'err'); }
    }

    async function installFunction(id) {
      const btn = document.getElementById(id === 'run_tool' ? 'installRunTool' : 'installSaveKnowledge');
      const statusEl = document.getElementById(id === 'run_tool' ? 'statusRunTool' : 'statusSaveKnowledge');
      const msg = document.getElementById('installMsg');
      btn.disabled = true; statusEl.textContent = '↻';
      try {
        const r = await fetch('/api/owui/install/' + id, { method: 'POST' });
        const d = await r.json();
        statusEl.textContent = d.ok ? '✓' : '✗';
        statusEl.style.color = d.ok ? '#a6e3a1' : '#f38ba8';
        msg.textContent = d.ok ? ('✓ Installed: ' + id.replace('_', ' ')) : ('✗ ' + d.message);
        msg.className = 'settings-msg ' + (d.ok ? 'ok' : 'err');
      } catch(e) {
        statusEl.textContent = '✗';
        msg.textContent = '✗ ' + e.message; msg.className = 'settings-msg err';
      } finally { btn.disabled = false; }
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
        let msg = 'Saved: ' + name + '.py';
        if (data.owui_sync) {
          msg += data.owui_sync.ok ? ' — ☁ synced to OpenWebUI' : ' — ⚠ OWUI sync failed: ' + data.owui_sync.message;
          if (data.owui_sync.ok) syncedTools.add(name);
        }
        setStatus(msg, data.owui_sync && !data.owui_sync.ok ? 'err' : 'ok');
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

    loadSettings().then(() => loadTools());
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


@app.get("/admin/functions/save_to_knowledge.py", response_class=PlainTextResponse)
async def get_save_to_knowledge_function():
    return PlainTextResponse(SAVE_TO_KNOWLEDGE_FUNCTION, media_type="text/x-python")


@app.get("/admin/functions/run_tool.py", response_class=PlainTextResponse)
async def get_run_tool_function():
    return PlainTextResponse(RUN_TOOL_FUNCTION, media_type="text/x-python")


@app.get("/api/tools")
async def list_tools():
    USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(
        p.stem for p in USER_TOOLS_DIR.glob("*.py") if not p.name.startswith("_")
    )
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
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseTool)
                    and obj is not BaseTool
                    and obj.__module__ == module_name
                ):
                    instance = obj()
                    entry["tools"].append(
                        {
                            "name": getattr(instance, "name", attr_name),
                            "description": getattr(instance, "description", ""),
                            "parameters": getattr(instance, "parameters", {}),
                        }
                    )
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
    tool_name = body.get("tool")  # which tool class (by name) to invoke
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
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseTool)
            and obj is not BaseTool
            and obj.__module__ == module_name
        ):
            inst = obj()
            if tool_name is None or getattr(inst, "name", None) == tool_name:
                instance = inst
                break

    if instance is None:
        raise HTTPException(
            status_code=404, detail=f"Tool '{tool_name}' not found in {name}.py"
        )

    try:
        result = await instance.execute(**params)
        return {
            "output": str(result.output) if hasattr(result, "output") else str(result)
        }
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
    (USER_TOOLS_DIR / f"{name}.py").write_text(code)
    s = get_settings()
    token = _session_token(request)
    sync_result = None
    if token:
        owui_url = s.get("owui_url", "http://open-webui:8080")
        ok, msg = await _owui_sync_one(name, owui_url, token)
        if ok:
            synced = s.get("synced_tools", [])
            if name not in synced:
                synced.append(name)
            s["synced_tools"] = synced
            write_settings(s)
        sync_result = {"ok": ok, "message": msg}
    return {"saved": True, "name": name, "owui_sync": sync_result}


@app.delete("/api/tools/{name}")
async def delete_tool(name: str, request: Request):
    if not name.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid tool name")
    path = USER_TOOLS_DIR / f"{name}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Tool not found")
    path.unlink()
    s = get_settings()
    token = _session_token(request)
    if token:
        await _owui_delete_one(name, s.get("owui_url", "http://open-webui:8080"), token)
        s["synced_tools"] = [t for t in s.get("synced_tools", []) if t != name]
        write_settings(s)
    return {"deleted": True, "name": name}


# ---------------------------------------------------------------------------
# OpenWebUI integration endpoints
# ---------------------------------------------------------------------------


@app.get("/api/owui/settings")
async def owui_get_settings(request: Request):
    s = get_settings()
    owui_url = s.get("owui_url", "http://open-webui:8080")
    token = _session_token(request)
    connected = False
    if token:
        try:
            import httpx as _hx

            async with _hx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"{owui_url}/api/v1/tools/",
                    headers={"Authorization": f"Bearer {token}"},
                )
                connected = r.status_code == 200
        except Exception:
            pass
    return {
        "owui_url": owui_url,
        "connected": connected,
        "synced_tools": s.get("synced_tools", []),
    }


@app.post("/api/owui/settings")
async def owui_save_settings(request: Request):
    body = await request.json()
    s = get_settings()
    if "owui_url" in body:
        s["owui_url"] = body["owui_url"].rstrip("/")
    write_settings(s)
    return {"saved": True}


@app.post("/api/owui/test")
async def owui_test(request: Request):
    s = get_settings()
    body = await request.json()
    owui_url = body.get("owui_url", s.get("owui_url", "http://open-webui:8080")).rstrip(
        "/"
    )
    token = _session_token(request)
    if not token:
        return {
            "ok": False,
            "message": "No session cookie — are you logged into OpenWebUI?",
        }
    import httpx as _hx

    try:
        async with _hx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{owui_url}/api/v1/tools/",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                return {
                    "ok": True,
                    "message": f"Connected — {len(r.json())} tool(s) registered in OpenWebUI",
                }
            return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/owui/sync-all")
async def owui_sync_all(request: Request):
    token = _session_token(request)
    if not token:
        raise HTTPException(
            status_code=401, detail="No session cookie — are you logged into OpenWebUI?"
        )
    s = get_settings()
    owui_url = s.get("owui_url", "http://open-webui:8080")
    USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    results, synced = {}, []
    for py_file in sorted(USER_TOOLS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        ok, msg = await _owui_sync_one(py_file.stem, owui_url, token)
        results[py_file.stem] = {"ok": ok, "message": msg}
        if ok:
            synced.append(py_file.stem)
    s["synced_tools"] = synced
    write_settings(s)
    return {"results": results}


@app.post("/api/owui/bootstrap")
async def owui_bootstrap(request: Request):
    """Verify the user's existing session cookie works against OpenWebUI."""
    token = _session_token(request)
    if not token:
        return {
            "ok": False,
            "message": "No OpenWebUI session cookie found — make sure you are logged into OpenWebUI first.",
        }
    s = get_settings()
    owui_url = s.get("owui_url", "http://open-webui:8080")
    import httpx as _hx

    try:
        async with _hx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{owui_url}/api/v1/tools/",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                return {
                    "ok": True,
                    "message": f"Session verified — {len(r.json())} tool(s) in OpenWebUI.",
                }
            return {
                "ok": False,
                "message": f"Session check failed: HTTP {r.status_code}",
            }
    except Exception as e:
        return {"ok": False, "message": f"Error: {e}"}


@app.post("/api/owui/install/{func_id}")
async def owui_install_function(func_id: str, request: Request):
    token = _session_token(request)
    if not token:
        raise HTTPException(
            status_code=401, detail="No session cookie — are you logged into OpenWebUI?"
        )
    s = get_settings()
    owui_url = s.get("owui_url", "http://open-webui:8080")
    if func_id == "run_tool":
        ok, msg = await _owui_install_fn(
            "run_tool", "Run Tool", RUN_TOOL_FUNCTION, owui_url, token
        )
    elif func_id == "save_to_knowledge":
        ok, msg = await _owui_install_fn(
            "save_to_knowledge",
            "Save to Knowledge",
            SAVE_TO_KNOWLEDGE_FUNCTION,
            owui_url,
            token,
        )
    else:
        raise HTTPException(status_code=404, detail="Unknown function")
    return {"ok": ok, "message": msg}


@app.on_event("startup")
async def startup_event():
    await clawdtalk_bridge.start()


@app.on_event("shutdown")
async def shutdown_event():
    await clawdtalk_bridge.stop()


@app.get("/api/clawdtalk/status")
async def clawdtalk_status():
    return clawdtalk_bridge.status()


@app.post("/api/clawdtalk/calls")
async def clawdtalk_create_call(request: Request):
    if not clawdtalk_bridge.enabled:
        raise HTTPException(
            status_code=503, detail="ClawdTalk bridge is not configured"
        )
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="Request body must be a JSON object"
        )
    if "to" not in payload:
        raise HTTPException(status_code=400, detail="Missing required field: to")
    return await clawdtalk_bridge.create_call(payload)


# ---------------------------------------------------------------------------
# Downloads — file browser for statements, CSVs, and other agent-saved files
# ---------------------------------------------------------------------------

_DOWNLOADS_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Downloads</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#1a1a1a}}
  h1{{font-size:1.4rem;margin-bottom:4px}}
  .sub{{color:#666;font-size:.9rem;margin-bottom:24px}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;border-bottom:2px solid #e5e7eb;padding:8px 12px;font-size:.8rem;color:#6b7280;text-transform:uppercase}}
  td{{padding:10px 12px;border-bottom:1px solid #f3f4f6;font-size:.95rem}}
  tr:hover td{{background:#f9fafb}}
  a{{color:#2563eb;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .size{{color:#9ca3af;font-size:.85rem}}
  .empty{{color:#9ca3af;padding:40px 0;text-align:center}}
  .dir{{font-weight:600;color:#374151}}
  .breadcrumb{{font-size:.9rem;margin-bottom:16px;color:#6b7280}}
  .breadcrumb a{{color:#2563eb}}
</style>
</head>
<body>
<h1>Downloads</h1>
<div class="sub">Files saved by OpenManus agents — statements, CSVs, and other exports.</div>
<div class="breadcrumb">{breadcrumb}</div>
<table>
<tr><th>Name</th><th>Size</th><th>Modified</th><th></th></tr>
{rows}
</table>
</body>
</html>"""


@app.get("/admin/downloads", response_class=HTMLResponse)
@app.get("/admin/downloads/{subpath:path}", response_class=HTMLResponse)
async def downloads_browser(request: Request, subpath: str = ""):
    import html as html_module
    import mimetypes
    from datetime import datetime

    rel = Path(subpath) if subpath else Path(".")
    target = (DOWNLOADS_DIR / rel).resolve()

    # Safety: never escape outside DOWNLOADS_DIR
    try:
        target.relative_to(DOWNLOADS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if target.is_file():
        # Serve the file for download
        from fastapi.responses import FileResponse

        mime, _ = mimetypes.guess_type(target.name)
        return FileResponse(
            path=str(target),
            filename=target.name,
            media_type=mime or "application/octet-stream",
        )

    # Directory listing
    parts = ["<a href='/admin/downloads'>downloads</a>"]
    so_far = ""
    for part in rel.parts:
        so_far = f"{so_far}/{part}" if so_far else part
        parts.append(
            f"<a href='/admin/downloads/{so_far}'>{html_module.escape(part)}</a>"
        )
    breadcrumb = " / ".join(parts)

    rows = []
    if rel != Path("."):
        parent = str(rel.parent) if rel.parent != Path(".") else ""
        up_href = f"/admin/downloads/{parent}" if parent else "/admin/downloads"
        rows.append(
            f"<tr><td><a href='{up_href}'>.. (up)</a></td><td></td><td></td><td></td></tr>"
        )

    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    for entry in entries:
        name = html_module.escape(entry.name)
        rel_path = (rel / entry.name) if subpath else entry.name
        href = f"/admin/downloads/{rel_path}"
        mod = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if entry.is_dir():
            rows.append(
                f"<tr><td class='dir'><a href='{href}'>{name}/</a></td><td></td><td class='size'>{mod}</td><td></td></tr>"
            )
        else:
            size = entry.stat().st_size
            size_str = (
                f"{size / 1024:.1f} KB"
                if size < 1_048_576
                else f"{size / 1_048_576:.1f} MB"
            )
            rows.append(
                f"<tr><td><a href='{href}'>{name}</a></td><td class='size'>{size_str}</td><td class='size'>{mod}</td><td><a href='{href}'>⬇ download</a></td></tr>"
            )

    if not entries:
        rows.append(
            "<tr><td colspan='4' class='empty'>No files yet. Ask the agent to download something.</td></tr>"
        )

    html = _DOWNLOADS_HTML.format(breadcrumb=breadcrumb, rows="\n".join(rows))
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Google Drive — OAuth setup + rclone sync
# ---------------------------------------------------------------------------

RCLONE_CONFIG_DIR = Path("/app/rclone-config")
RCLONE_CONFIG_FILE = RCLONE_CONFIG_DIR / "rclone.conf"
RCLONE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

_GDRIVE_REMOTE = "gdrive"
_GDRIVE_REDIRECT = "https://manus.designflow.app/api/gdrive/callback"
_GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_GDRIVE_FOLDER = os.environ.get("GDRIVE_FOLDER", "OpenManus")


def _gdrive_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def _gdrive_client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def _gdrive_configured() -> bool:
    if not RCLONE_CONFIG_FILE.exists():
        return False
    content = RCLONE_CONFIG_FILE.read_text()
    return "[gdrive]" in content and "refresh_token" in content


def _rclone_available() -> bool:
    return _subprocess.run(["rclone", "version"], capture_output=True).returncode == 0


_GDRIVE_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Google Drive</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 24px;color:#1a1a1a}}
  h1{{font-size:1.4rem}}
  .card{{border:1px solid #e5e7eb;border-radius:10px;padding:24px;margin:20px 0}}
  .status{{display:flex;align-items:center;gap:10px;font-size:1rem;margin-bottom:16px}}
  .dot{{width:12px;height:12px;border-radius:50%}}
  .green{{background:#22c55e}} .red{{background:#ef4444}} .grey{{background:#9ca3af}}
  .btn{{display:inline-block;padding:10px 20px;border-radius:6px;border:none;cursor:pointer;font-size:.95rem;text-decoration:none}}
  .btn-primary{{background:#2563eb;color:#fff}}
  .btn-secondary{{background:#f3f4f6;color:#374151;border:1px solid #d1d5db}}
  .btn:hover{{opacity:.9}}
  .note{{font-size:.85rem;color:#6b7280;margin-top:12px}}
  .error{{background:#fef2f2;border:1px solid #fecaca;padding:12px;border-radius:6px;color:#dc2626;font-size:.9rem}}
  .success{{background:#f0fdf4;border:1px solid #bbf7d0;padding:12px;border-radius:6px;color:#16a34a;font-size:.9rem}}
  code{{background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:.85rem}}
  .setup-steps{{font-size:.9rem;line-height:1.8;color:#374151}}
  .setup-steps a{{color:#2563eb}}
</style>
</head>
<body>
<h1>Google Drive</h1>
<div class="card">
  <div class="status">
    <div class="dot {dot_class}"></div>
    <strong>{status_text}</strong>
  </div>
  {body}
</div>
{extra}
</body>
</html>"""


@app.get("/admin/gdrive", response_class=HTMLResponse)
async def gdrive_page():
    client_id = _gdrive_client_id()
    client_secret = _gdrive_client_secret()
    rclone_ok = _rclone_available()
    configured = _gdrive_configured()

    if not rclone_ok:
        return HTMLResponse(
            _GDRIVE_PAGE.format(
                dot_class="red",
                status_text="rclone not installed",
                body="<div class='error'>rclone is not installed in this container. Rebuild the Docker image to include it.</div>",
                extra="",
            )
        )

    if not client_id or not client_secret:
        return HTMLResponse(
            _GDRIVE_PAGE.format(
                dot_class="grey",
                status_text="Not configured",
                body="""
<div class='setup-steps'>
  <p>To connect Google Drive you need a Google OAuth client set up for Drive access.</p>
  <ol>
    <li>Go to <a href='https://console.cloud.google.com/apis/credentials' target='_blank'>Google Cloud Console → Credentials</a></li>
    <li>Create or select an OAuth 2.0 Client ID (Web application)</li>
    <li>Add <code>https://manus.designflow.app/api/gdrive/callback</code> as an Authorized redirect URI</li>
    <li>Enable the <strong>Google Drive API</strong> in your project</li>
    <li>Add <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> as environment variables in Coolify</li>
    <li>Redeploy — then come back here to connect</li>
  </ol>
  <p class='note'>These are the same credentials already used for Google Sign-In to this app. You just need to also enable the Drive API and add the redirect URI.</p>
</div>""",
                extra="",
            )
        )

    if configured:
        folder = _GDRIVE_FOLDER
        return HTMLResponse(
            _GDRIVE_PAGE.format(
                dot_class="green",
                status_text="Connected to Google Drive",
                body=f"""
<p>Downloads sync automatically to <strong>{folder}/</strong> in your Google Drive after every Fidelity download.</p>
<a href='/api/gdrive/sync' class='btn btn-primary' style='margin-right:8px'>Sync now</a>
<a href='/api/gdrive/disconnect' class='btn btn-secondary'>Disconnect</a>
<p class='note'>Destination folder: My Drive / {folder}</p>""",
                extra="",
            )
        )

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={_GDRIVE_REDIRECT}"
        "&response_type=code"
        f"&scope={_GDRIVE_SCOPE}"
        "&access_type=offline"
        "&prompt=consent"
    )
    return HTMLResponse(
        _GDRIVE_PAGE.format(
            dot_class="grey",
            status_text="Not connected",
            body=f"""
<p>Connect your Google Drive so Fidelity downloads save there automatically.</p>
<a href='{auth_url}' class='btn btn-primary'>Connect Google Drive</a>
<p class='note'>You'll be asked to sign in with Google and grant access to Google Drive.</p>""",
            extra="",
        )
    )


@app.get("/api/gdrive/callback")
async def gdrive_callback(code: str = "", error: str = ""):
    if error:
        return HTMLResponse(
            f"<h2>Error: {error}</h2><p><a href='/admin/gdrive'>Back</a></p>"
        )
    if not code:
        return HTMLResponse(
            "<h2>Missing code</h2><p><a href='/admin/gdrive'>Back</a></p>"
        )

    client_id = _gdrive_client_id()
    client_secret = _gdrive_client_secret()

    import httpx
    import json as _json
    from datetime import datetime, timezone, timedelta

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": _GDRIVE_REDIRECT,
                "grant_type": "authorization_code",
            },
        )

    if resp.status_code != 200:
        return HTMLResponse(
            f"<h2>Token exchange failed</h2><pre>{resp.text}</pre><p><a href='/admin/gdrive'>Back</a></p>"
        )

    tokens = resp.json()
    expiry = (
        datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))
    ).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")
    token_json = _json.dumps(
        {
            "access_token": tokens["access_token"],
            "token_type": tokens.get("token_type", "Bearer"),
            "refresh_token": tokens.get("refresh_token", ""),
            "expiry": expiry,
        }
    )

    RCLONE_CONFIG_FILE.write_text(
        f"[gdrive]\n"
        f"type = drive\n"
        f"client_id = {client_id}\n"
        f"client_secret = {client_secret}\n"
        f"scope = drive\n"
        f"token = {token_json}\n"
    )

    return HTMLResponse("""
<html><head><meta charset='utf-8'>
<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;text-align:center}
.check{font-size:3rem}.btn{display:inline-block;padding:10px 20px;background:#2563eb;color:#fff;border-radius:6px;text-decoration:none;margin-top:16px}</style>
</head><body>
<div class='check'>✅</div>
<h2>Google Drive connected!</h2>
<p>Fidelity downloads will now sync to your Google Drive automatically.</p>
<a href='/admin/gdrive' class='btn'>Back to Google Drive settings</a>
</body></html>""")


@app.get("/api/gdrive/status")
async def gdrive_status():
    return {
        "configured": _gdrive_configured(),
        "rclone_available": _rclone_available(),
        "folder": _GDRIVE_FOLDER,
        "client_id_set": bool(_gdrive_client_id()),
    }


@app.get("/api/gdrive/sync")
async def gdrive_sync():
    if not _gdrive_configured():
        return HTMLResponse(
            "<h2>Not connected</h2><p><a href='/admin/gdrive'>Set up Google Drive first</a></p>"
        )
    if not _rclone_available():
        return JSONResponse({"ok": False, "error": "rclone not available"})

    proc = _subprocess.run(
        [
            "rclone",
            "copy",
            str(DOWNLOADS_DIR),
            f"{_GDRIVE_REMOTE}:{_GDRIVE_FOLDER}",
            "--config",
            str(RCLONE_CONFIG_FILE),
            "--create-empty-src-dirs",
            "-v",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    ok = proc.returncode == 0
    if "text/html" in (proc.stderr or ""):
        return JSONResponse(
            {"ok": ok, "stdout": proc.stdout[-3000:], "stderr": proc.stderr[-3000:]}
        )
    return HTMLResponse(f"""
<html><head><meta charset='utf-8'>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 24px}}
pre{{background:#f3f4f6;padding:16px;border-radius:6px;overflow-x:auto;font-size:.85rem}}
.btn{{display:inline-block;padding:10px 20px;background:#2563eb;color:#fff;border-radius:6px;text-decoration:none}}</style>
</head><body>
<h2>{"✅ Sync complete" if ok else "❌ Sync failed"}</h2>
<pre>{proc.stdout or ""}{proc.stderr or ""}</pre>
<a href='/admin/gdrive' class='btn'>Back</a>
</body></html>""")


@app.get("/api/gdrive/disconnect")
async def gdrive_disconnect():
    if RCLONE_CONFIG_FILE.exists():
        RCLONE_CONFIG_FILE.unlink()
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/admin/gdrive")


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


def _fmt_model_name(raw_name: str, pricing: dict) -> str:
    # Strip "Provider: " prefix (e.g. "DeepSeek: DeepSeek V4 Pro" → "DeepSeek V4 Pro")
    name = raw_name.split(": ", 1)[-1] if ": " in raw_name else raw_name
    # Append $/M input / $/M output pricing
    try:
        inp = float(pricing.get("prompt", 0)) * 1_000_000
        out = float(pricing.get("completion", 0)) * 1_000_000

        def _p(v):
            if v <= 0:
                return "?"
            s = f"{v:.3g}"
            return f"${s}"

        name = f"{name} {_p(inp)}/{_p(out)}"
    except Exception:
        pass
    return name


@app.get("/v1/models")
async def list_models():
    import os
    import httpx

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://openrouter.ai/api/v1/models/user",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        for m in data.get("data", []):
            m["name"] = _fmt_model_name(
                m.get("name", m.get("id", "")),
                m.get("pricing", {}),
            )
        return data


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
                content={
                    "error": {
                        "message": "No user message found",
                        "type": "invalid_request_error",
                    }
                },
            )

        logger.info(
            f"Running Manus agent | model={requested_model} "
            f"stream={stream} temperature={requested_temperature} "
            f"max_tokens={requested_max_tokens} | prompt={prompt[:100]}..."
        )

        # Inject user tools. When OpenWebUI sends a `tools` array (user selected specific tools
        # via the ⚡ tool selector in chat), only inject those. Otherwise inject all — backwards
        # compat for when OpenWebUI integration is not configured.
        _owui_selected = {
            t.get("function", {}).get("name", "")
            for t in (body.get("tools") or [])
            if isinstance(t, dict) and t.get("type") == "function"
        }
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model_id = requested_model or "manus"

        if stream:
            # Real streaming: intercept loguru agent logs via a temporary sink and
            # push each meaningful line to a queue so the SSE generator can yield
            # progress chunks while the agent is still running.
            progress_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

            async def _run_with_sink() -> str:
                try:
                    from loguru import logger as _loguru
                except ImportError:
                    return await run_manus_agent(
                        messages=messages,
                        requested_model=requested_model,
                        requested_temperature=requested_temperature,
                        requested_max_tokens=requested_max_tokens,
                        selected_tool_names=_owui_selected or None,
                    )

                _MARKERS = (
                    "Executing step",
                    "✨",
                    "🛠️",
                    "🧰",
                    "🎯",
                    "🚨",
                    "Token limit",
                    "stuck",
                    "Error",
                    "error",
                )

                def _sink(record):
                    msg = record["message"].strip()
                    if any(m in msg for m in _MARKERS):
                        try:
                            progress_q.put_nowait(msg)
                        except Exception:
                            pass

                sink_id = _loguru.add(
                    _sink,
                    filter=lambda r: r["name"].startswith("app."),
                    format="{message}",
                    level="INFO",
                )
                try:
                    return await run_manus_agent(
                        messages=messages,
                        requested_model=requested_model,
                        requested_temperature=requested_temperature,
                        requested_max_tokens=requested_max_tokens,
                        selected_tool_names=_owui_selected or None,
                    )
                finally:
                    _loguru.remove(sink_id)
                    await progress_q.put(None)  # sentinel: agent finished

            agent_task = asyncio.create_task(_run_with_sink())

            def _fmt(raw: str) -> str | None:
                import re as _re

                if "Executing step" in raw:
                    m = _re.search(r"Executing step (\d+)/\d+", raw)
                    return f"\n**Step {m.group(1)}**\n" if m else None
                if "✨" in raw and "thoughts" in raw:
                    thought = raw.split("thoughts:", 1)[-1].strip()
                    return f"💭 {thought}\n" if thought else None
                if "🧰" in raw and "prepared" in raw:
                    tools = raw.split("prepared:", 1)[-1].strip()
                    return f"🔧 Using: {tools}\n"
                if "🎯" in raw:
                    # Truncate long tool results to keep the stream readable
                    truncated = raw[:400] + ("…" if len(raw) > 400 else "")
                    return f"{truncated}\n"
                if "🚨" in raw or "Token limit" in raw:
                    return f"❌ {raw}\n"
                # Surface explicit tool errors that aren't already caught above
                if ("Error:" in raw or "error:" in raw) and "🎯" not in raw:
                    return f"⚠️ {raw[:300]}\n"
                return None

            async def event_stream():
                def _chunk(delta: dict, finish_reason=None) -> str:
                    return f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_id, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}]})}\n\n"

                yield _chunk({"role": "assistant"})

                while True:
                    try:
                        raw = await asyncio.wait_for(progress_q.get(), timeout=300.0)
                    except asyncio.TimeoutError:
                        yield _chunk(
                            {"content": "\n⏱️ Timed out waiting for agent step.\n"}
                        )
                        break
                    if raw is None:
                        break
                    line = _fmt(raw)
                    if line:
                        yield _chunk({"content": line})

                try:
                    answer = await asyncio.wait_for(agent_task, timeout=60.0)
                except Exception as e:
                    answer = f"Agent error: {e}"

                if answer:
                    yield _chunk({"content": f"\n---\n\n{answer}"})
                yield _chunk({}, finish_reason="stop")
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        answer = await run_manus_agent(
            messages=messages,
            requested_model=requested_model,
            requested_temperature=requested_temperature,
            requested_max_tokens=requested_max_tokens,
            selected_tool_names=_owui_selected or None,
        )

        try:
            from app.llm import LLM

            llm = LLM()
            if requested_model and requested_model not in ("manus", "openmanus"):
                llm.model = requested_model
            prompt_tokens = llm.count_tokens(
                " ".join(m.get("content", "") or "" for m in messages)
            )
            completion_tokens = llm.count_tokens(answer or "")
        except Exception:
            prompt_tokens = len(prompt.split())
            completion_tokens = len(answer.split()) if answer else 0

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

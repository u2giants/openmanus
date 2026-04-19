"""
Fidelity Browser Tool — OpenManus user tool for Fidelity.com automation.

Works with the visible Chromium browser in the noVNC window. You log in
manually; these tools take over from there using the same browser session.

Requires: the novnc container to be running with Chromium on CDP port 9222.
The CDP proxy inside novnc makes it reachable at http://novnc:9223.
"""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.tool.base import BaseTool, ToolResult

# All sync Playwright operations run in this single-thread pool so they
# never block the async event loop and never run concurrently.
_pool = ThreadPoolExecutor(max_workers=1)

_CDP_URL = os.environ.get("FIDELITY_CDP_URL", "http://novnc:9223")
_DOWNLOADS_DIR = "/app/downloads/Fidelity"
_RCLONE_CONFIG = "/app/rclone-config/rclone.conf"
_GDRIVE_FOLDER = os.environ.get("GDRIVE_FOLDER", "OpenManus")


def _gdrive_sync_if_configured() -> dict:
    """If Google Drive is set up, sync /app/downloads to it. Silent if not configured."""
    import subprocess
    from pathlib import Path

    config = Path(_RCLONE_CONFIG)
    if not config.exists() or "refresh_token" not in config.read_text():
        return {"gdrive_sync": "skipped (not configured)"}
    try:
        proc = subprocess.run(
            ["rclone", "copy", "/app/downloads", f"gdrive:{_GDRIVE_FOLDER}",
             "--config", _RCLONE_CONFIG, "--create-empty-src-dirs"],
            capture_output=True, text=True, timeout=60,
        )
        return {
            "gdrive_sync": "ok" if proc.returncode == 0 else "failed",
            "gdrive_returncode": proc.returncode,
        }
    except Exception as exc:
        return {"gdrive_sync": f"error: {exc}"}


def _run(fn) -> Any:
    """Run a sync callable in the thread pool and return its result."""
    future = _pool.submit(fn)
    return future.result(timeout=300)


def _ok(data: dict) -> ToolResult:
    return ToolResult(output=json.dumps(data, indent=2, ensure_ascii=False))


def _err(msg: str) -> ToolResult:
    return ToolResult(error=msg)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — attach & wait for login
# ─────────────────────────────────────────────────────────────────────────────

class FidelityAttachAndWait(BaseTool):
    name: str = "fidelity_attach_and_wait"
    description: str = (
        "Connect OpenManus to the visible Chromium browser in noVNC and wait until "
        "you have finished logging into Fidelity.com manually. "
        "Call this first before any other Fidelity tool. "
        "Once this returns success you are ready to download statements or positions."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "timeout_seconds": {
                "type": "integer",
                "description": "How long to wait for you to finish logging in (default 600 = 10 minutes).",
            },
            "cdp_url": {
                "type": "string",
                "description": (
                    "CDP HTTP endpoint of the visible browser. "
                    "Default is http://novnc:9223 (correct for the Docker/noVNC setup). "
                    "Use http://localhost:9222 for local development."
                ),
            },
        },
        "required": [],
    }

    async def execute(
        self,
        timeout_seconds: int = 600,
        cdp_url: str = _CDP_URL,
        **_,
    ) -> ToolResult:
        def _sync():
            from playwright.sync_api import sync_playwright
            from fidelity_mcp.browser import FidelityBrowser

            fb = FidelityBrowser()
            fb.attach(cdp_url=cdp_url)
            info = fb.current_page()
            result = fb.wait_for_manual_login(timeout_seconds=timeout_seconds)
            fb.close()
            return {**info, **result}

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_pool, _sync)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — download statements
# ─────────────────────────────────────────────────────────────────────────────

class FidelityDownloadStatements(BaseTool):
    name: str = "fidelity_download_statements"
    description: str = (
        "Download Fidelity account statement PDFs for a given month. "
        "Navigates to the Fidelity Document Hub in the visible browser, "
        "finds statements matching the requested month, and saves them as PDF files. "
        "You must be logged in first (use fidelity_attach_and_wait). "
        "Example: download statements for March 2026 into ./Statements/Fidelity."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "date_yyyy_mm": {
                "type": "string",
                "description": "Month to download in YYYY/MM format, e.g. '2026/03' for March 2026.",
            },
            "out_dir": {
                "type": "string",
                "description": "Directory to save PDF files. Default saves to the Downloads folder accessible at manus.designflow.app/admin/downloads.",
            },
            "account_contains": {
                "type": "string",
                "description": (
                    "Optional: only download statements for accounts whose name or number "
                    "contains this text, e.g. 'Brokerage' or the last 4 digits of an account."
                ),
            },
            "cdp_url": {
                "type": "string",
                "description": "CDP endpoint (default: http://novnc:9223).",
            },
        },
        "required": ["date_yyyy_mm", "out_dir"],
    }

    async def execute(
        self,
        date_yyyy_mm: str,
        out_dir: str = _DOWNLOADS_DIR,
        account_contains: str = "",
        cdp_url: str = _CDP_URL,
        **_,
    ) -> ToolResult:
        def _sync():
            from fidelity_mcp.browser import FidelityBrowser

            fb = FidelityBrowser()
            fb.attach(cdp_url=cdp_url)
            result = fb.download_statements(
                date_yyyy_mm=date_yyyy_mm,
                out_dir=out_dir,
                account_contains=account_contains.strip() or None,
            )
            fb.close()
            result.update(_gdrive_sync_if_configured())
            return result

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_pool, _sync)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — download positions CSV
# ─────────────────────────────────────────────────────────────────────────────

class FidelityDownloadPositions(BaseTool):
    name: str = "fidelity_download_positions"
    description: str = (
        "Download a CSV of current Fidelity portfolio positions (holdings). "
        "Navigates to the Fidelity Positions page in the visible browser and "
        "clicks the export/download button to save a CSV file. "
        "You must be logged in first (use fidelity_attach_and_wait)."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "out_dir": {
                "type": "string",
                "description": "Directory to save the CSV file. Default saves to the Downloads folder accessible at manus.designflow.app/admin/downloads.",
            },
            "cdp_url": {
                "type": "string",
                "description": "CDP endpoint (default: http://novnc:9223).",
            },
        },
        "required": ["out_dir"],
    }

    async def execute(
        self,
        out_dir: str = _DOWNLOADS_DIR,
        cdp_url: str = _CDP_URL,
        **_,
    ) -> ToolResult:
        def _sync():
            from fidelity_mcp.browser import FidelityBrowser

            fb = FidelityBrowser()
            fb.attach(cdp_url=cdp_url)
            result = fb.download_positions_csv(out_dir=out_dir)
            fb.close()
            result.update(_gdrive_sync_if_configured())
            return result

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_pool, _sync)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4 — navigate to any Fidelity page
# ─────────────────────────────────────────────────────────────────────────────

class FidelityGoTo(BaseTool):
    name: str = "fidelity_goto"
    description: str = (
        "Navigate the visible Fidelity browser to any URL. "
        "Useful for going to a specific Fidelity page or for debugging. "
        "You must be attached first (use fidelity_attach_and_wait)."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL to navigate to.",
            },
            "dump_html": {
                "type": "boolean",
                "description": "If true, also return the first 5000 characters of the page HTML.",
            },
            "cdp_url": {
                "type": "string",
                "description": "CDP endpoint (default: http://novnc:9223).",
            },
        },
        "required": ["url"],
    }

    async def execute(
        self,
        url: str,
        dump_html: bool = False,
        cdp_url: str = _CDP_URL,
        **_,
    ) -> ToolResult:
        def _sync():
            from fidelity_mcp.browser import FidelityBrowser

            fb = FidelityBrowser()
            fb.attach(cdp_url=cdp_url)
            result = fb.goto(url)
            if dump_html:
                snap = fb.dump_dom_snapshot(max_chars=5000)
                result["html_excerpt"] = snap.get("html_excerpt", "")
            fb.close()
            return result

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_pool, _sync)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

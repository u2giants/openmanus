from __future__ import annotations

import atexit
import json
import os
import shlex
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP

from .browser import FidelityBrowser


mcp = FastMCP("fidelity-mcp")
_browser = FidelityBrowser()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@atexit.register
def _cleanup() -> None:
    _browser.close()


@mcp.tool()
def launch_visible_chrome(
    chrome_path: str = "google-chrome",
    remote_debugging_port: int = 9222,
    user_data_dir: str = os.path.expanduser("~/.chrome-fidelity-debug"),
    start_url: str = "https://digital.fidelity.com/",
    extra_args: str = "",
) -> str:
    """Launch a visible Chromium browser for noVNC/manual login.

    Use this first if you do not already have a browser open in the VNC desktop.
    After the browser appears, log in manually, then run attach_visible_browser.
    """
    cmd = [
        chrome_path,
        f"--remote-debugging-port={remote_debugging_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    if extra_args.strip():
        cmd.extend(shlex.split(extra_args))

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _json({
        "launched": True,
        "command": cmd,
        "next_step": f"Open the VNC desktop, log in to Fidelity if needed, then attach to http://127.0.0.1:{remote_debugging_port}",
    })


@mcp.tool()
def attach_visible_browser(
    cdp_url: str = "http://127.0.0.1:9222",
    context_index: int = 0,
    page_index: int = 0,
) -> str:
    """Attach Playwright to the visible Chromium browser you can see over noVNC."""
    info = _browser.attach(cdp_url=cdp_url, context_index=context_index, page_index=page_index)
    return _json(info.__dict__)


@mcp.tool()
def list_open_pages() -> str:
    """List currently open tabs/pages in the attached visible browser."""
    return _json({"pages": _browser.list_pages()})


@mcp.tool()
def use_open_page(context_index: int = 0, page_index: int = 0) -> str:
    """Switch the active attached page to a different already-open browser tab."""
    return _json(_browser.use_page(context_index=context_index, page_index=page_index))


@mcp.tool()
def wait_for_manual_fidelity_login(timeout_seconds: int = 600) -> str:
    """Wait for you to finish logging in manually in the visible browser tab."""
    return _json(_browser.wait_for_manual_login(timeout_seconds=timeout_seconds))


@mcp.tool()
def current_page() -> str:
    """Return the title and URL of the currently attached page."""
    return _json(_browser.current_page())


@mcp.tool()
def open_fidelity_documents_hub() -> str:
    """Open Fidelity Document Hub in the currently attached browser tab."""
    return _json(_browser.open_documents_hub())


@mcp.tool()
def open_fidelity_positions_page() -> str:
    """Open Fidelity positions page in the currently attached browser tab."""
    return _json(_browser.open_positions_page())


@mcp.tool()
def download_fidelity_statements(
    date_yyyy_mm: str,
    out_dir: str = "./Statements",
    account_contains: str = "",
) -> str:
    """Download Fidelity statements for the requested month.

    Example date_yyyy_mm: 2026/03
    Optional account_contains can narrow results to rows containing text like
    'Brokerage' or the last few account digits.
    """
    account_filter = account_contains.strip() or None
    return _json(_browser.download_statements(date_yyyy_mm=date_yyyy_mm, out_dir=out_dir, account_contains=account_filter))


@mcp.tool()
def download_fidelity_positions_csv(out_dir: str = "./Statements") -> str:
    """Download Fidelity's positions CSV from the currently attached browser session."""
    return _json(_browser.download_positions_csv(out_dir=out_dir))


@mcp.tool()
def goto_url(url: str) -> str:
    """Open any URL in the currently attached browser tab."""
    return _json(_browser.goto(url))


@mcp.tool()
def dump_page_html(max_chars: int = 25000) -> str:
    """Return a trimmed HTML snapshot for debugging difficult pages."""
    return _json(_browser.dump_dom_snapshot(max_chars=max_chars))


@mcp.tool()
def run_import_fidelity(
    raw_args: str,
    binary_path: str = "import-fidelity",
    working_dir: str = ".",
    timeout_seconds: int = 1800,
) -> str:
    """Run the import-fidelity CLI with arbitrary arguments.

    This is optional. Install the import-fidelity binary separately if you want
    account activity export jobs.
    Example raw_args: --help
    """
    cmd = [binary_path, *shlex.split(raw_args)]
    completed = subprocess.run(
        cmd,
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return _json({
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
    })


@mcp.tool()
def close_attached_browser() -> str:
    """Disconnect Playwright from the visible browser session."""
    _browser.close()
    return _json({"closed": True})


if __name__ == "__main__":
    mcp.run(transport="stdio")

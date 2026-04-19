# Fidelity MCP — Visible Browser Integration

Attaches OpenManus to the **visible Chromium session running in noVNC** so you can
log into Fidelity.com manually and then hand off automation without any hidden browser.

---

## Files

| File | Purpose |
|---|---|
| `fidelity_mcp/__init__.py` | Package marker |
| `fidelity_mcp/browser.py` | `FidelityBrowser` — sync Playwright CDP attach + Fidelity automation |
| `fidelity_mcp/server.py` | FastMCP server exposing 13 tools via `stdio` |
| `launch_visible_chrome.sh` | Helper to launch Chrome with remote debugging (local dev) |
| `config.toml` | Updated with `[mcp] server_reference = "fidelity_mcp.server"` |

---

## Dependencies

Playwright and MCP are already in the Docker image (`requirements.txt` + Dockerfile).

For local development:

```bash
pip install playwright mcp
playwright install chromium
```

---

## Docker / noVNC workflow (normal path)

### 1. Start the stack

```bash
docker compose up -d
```

Chromium starts automatically in the `novnc` container on port 9222.
The CDP proxy makes it reachable from `openmanus-backend` at:
- HTTP discovery: `http://novnc:9223`
- WebSocket: `ws://novnc:9224`

### 2. Open noVNC

```
https://vnc.designflow.app
```

### 3. Log in manually

In the noVNC Chromium window, go to `https://www.fidelity.com` and complete login
(including 2FA). The automation will wait for you.

### 4. Start OpenManus in MCP mode

Inside the container:

```bash
cd /app
python run_mcp.py
```

`config/config.toml` points MCP at `fidelity_mcp.server`.

### 5. Send prompts

```
Attach to the visible browser and list my open pages.
```
```
Wait for my Fidelity login to complete.
```
```
Open Fidelity Document Hub and download all statements for 2026/03 into ./Statements/Fidelity.
```
```
Download the positions CSV into ./Statements/Fidelity.
```

---

## Local dev workflow (outside Docker)

### 1. Launch Chrome with remote debugging

```bash
./launch_visible_chrome.sh
# Override: CHROME_PATH=/usr/bin/chromium PORT=9222 ./launch_visible_chrome.sh
```

### 2. Log into Fidelity in that Chrome window.

### 3. Run MCP server

```bash
python run_mcp.py
```

### 4. Attach

```
Attach to the visible browser with cdp_url http://localhost:9222.
```

---

## Tool reference

| Tool | Description |
|---|---|
| `launch_visible_chrome` | Launch Chrome with remote debugging (local dev / fallback) |
| `attach_visible_browser` | Connect to running Chromium via CDP |
| `list_open_pages` | List all tabs: context_index, page_index, title, URL |
| `use_open_page` | Switch active page to given context/page indices |
| `current_page` | Title + URL of current page |
| `wait_for_manual_fidelity_login` | Poll until Fidelity login detected (up to N seconds) |
| `open_fidelity_documents_hub` | Navigate to Document Hub |
| `open_fidelity_positions_page` | Navigate to Positions page |
| `download_fidelity_statements` | Download statement PDFs for YYYY/MM, save to out_dir |
| `download_fidelity_positions_csv` | Download positions CSV export |
| `goto_url` | Navigate to any URL |
| `dump_page_html` | Return page HTML excerpt (debug) |
| `run_import_fidelity` | Shell out to `import-fidelity` binary (optional) |
| `close_attached_browser` | Release CDP connection without closing the browser |

---

## Example prompts

```
Launch the visible Chrome browser for Fidelity.
```
```
Attach to the visible browser and list my open pages.
```
```
Open Fidelity Document Hub and download all statements for 2026/03 into ./Statements/Fidelity.
```
```
Download only statements for account containing "X12345" for 2026/03 into ./Statements/Fidelity.
```
```
Download the positions CSV into ./Statements/Fidelity.
```
```
Go to https://digital.fidelity.com/ftgw/digital/portfolio/summary and show me the page HTML.
```

---

## Troubleshooting

**"No browser page is attached"** — Call `attach_visible_browser` first.

**CDP connection refused** — Check `http://novnc:9223/json/version` (Docker)
or `http://localhost:9222/json/version` (local). Chrome must be running.

**"There are no statements"** — Correct behaviour; Fidelity returned no data for
that year/month combination.

**Positions download button not found** — Fidelity redesigns their UI occasionally.
Use `dump_page_html` to inspect the page and identify the current selector.

**`import-fidelity` not found** — Optional tool. Install the binary separately;
the MCP tool returns a clear error when it's absent.

# OpenManus + Fidelity MCP starter pack

This package gives OpenManus a small set of Fidelity tools that work with a **visible Chromium browser** you can watch in **noVNC**.

## What this does

- launches a visible Chrome/Chromium window with remote debugging enabled
- lets **you** log in manually in the browser you can see
- lets OpenManus attach to that exact same browser session
- downloads Fidelity statement PDFs for a month like `2026/03`
- downloads the Fidelity positions CSV
- optionally runs `import-fidelity` if you install that CLI separately

## Why I built it this way

`fidelity-api` already has working Fidelity logic, including statement downloads, but it launches its own Playwright-controlled Firefox browser by default. That is not a good fit for your noVNC workflow, where you need to **see the browser and help with login**. So this starter pack adapts the useful Fidelity logic into a tool server that attaches to a **visible Chromium session** instead.

## Folder to copy into OpenManus

Copy the `fidelity_mcp` folder into the root of your OpenManus repo.

Example:

```bash
OpenManus/
  fidelity_mcp/
    __init__.py
    browser.py
    server.py
```

## Install steps inside your OpenManus environment

From your OpenManus folder:

```bash
pip install -r fidelity_mcp_requirements.txt
playwright install chromium
```

Create `fidelity_mcp_requirements.txt` with this content:

```txt
mcp>=1.0.0
playwright>=1.40.0
```

You can also just run:

```bash
pip install mcp playwright
playwright install chromium
```

## OpenManus config

In `config/config.toml`, set:

```toml
[mcp]
server_reference = "fidelity_mcp.server"
```

There is also an example file in this package called `openmanus.config.patch.toml`.

## Your daily workflow

### 1) Start OpenManus MCP mode

```bash
python run_mcp.py --interactive
```

### 2) In OpenManus, ask it to launch the visible browser

Tell it:

```text
Launch the visible Chrome browser for Fidelity.
```

That calls `launch_visible_chrome`.

### 3) In noVNC, log in manually

Use the visible browser window. If Fidelity asks for username, password, text code, authenticator code, or any weird prompt, do it yourself.

### 4) Tell OpenManus to attach

```text
Attach to the visible browser and list my open pages.
```

If needed:

```text
Switch to the Fidelity tab.
```

### 5) Tell it what to download

Examples:

```text
Open Fidelity Document Hub and download all statements for 2026/03 into ./Statements/Fidelity.
```

```text
Download the positions CSV into ./Statements/Fidelity.
```

## Useful tool sequence

For statement downloads, the most reliable sequence is:

1. `launch_visible_chrome`
2. `attach_visible_browser`
3. `wait_for_manual_fidelity_login`
4. `open_fidelity_documents_hub`
5. `download_fidelity_statements`

## If you want account activity exports too

This package includes a generic `run_import_fidelity` tool, but **you must install the `import-fidelity` binary yourself**.

That project is separate and is mainly for account activity / ticker downloads, not statement PDF downloads.

## Limitations

- I could not test against your real Fidelity account here.
- Fidelity changes its HTML and labels over time, so selectors may eventually need touching up.
- This package is focused on your visible-browser/noVNC workflow, not a hidden background browser.

## If Chrome is not called `google-chrome`

Common alternate paths:

- `chromium`
- `chromium-browser`
- `/usr/bin/google-chrome`
- `/usr/bin/chromium`

You can pass a custom `chrome_path` to `launch_visible_chrome`.

# Codex Daemon Bridge

[![English (current)](https://img.shields.io/badge/English-8C2F39?style=flat-square)](README.md) [![简体中文](https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-555555?style=flat-square)](README.zh-CN.md)

Coordinate independent Codex App Server sessions across providers through MCP.
Create provider launchers, discover models, and delegate tasks across independent daemons.

## Prerequisites

- Linux with Python 3.11+ and the `codex` CLI (compatible with Codex 0.167.2).
- Provider access through an API key environment variable or an existing configured Codex home.

## Install

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps .
source .venv/bin/activate
```

## Open the manager

```bash
codexd-bridge
```

The terminal interface is the main entry point. Use it to:

- Create a provider or attach an existing Codex home.
- Check, start and stop a daemon; verify access and connect the bridge.
- Browse provider models and test a selected model and reasoning level.
- Create, read, continue and fork sessions; add instructions to an active turn.
- Install a `codex-NAME` launcher or open that provider's native Codex interface.

Use **Tab / Shift+Tab** to move, **Enter** to choose, **Ctrl+S** to submit,
**Esc** to go back, and **Ctrl+Q** to exit. All workflows support keyboard
navigation. The minimum terminal size is 60 columns by 18 rows.

New providers reference an API key environment variable already exported in the
terminal; enter its **name**, such as `DEMO_API_KEY`, in the form. The bridge
never saves the key value. Supply the provider's endpoint, model ID and documented
context limit. Setup verifies a real response before enabling the route and
installing its launcher. If setup is incomplete, use **Retry connection** or
**Connect bridge** and **Install launcher** from the provider manager.

The interface shows actual setup stages and keeps running while daemon requests
complete. **Open Codex** returns to the manager when the native interface closes.
Stopping a daemon requires confirmation. Unattended delegation is enabled only
when explicitly selected under **Connect bridge**.

For a different registry, run `codexd-bridge --config /absolute/bridge.toml ui`.
Command-line operations remain available for automation through `--help`.
MCP clients use `codexd-bridge serve`; the no-argument entry also preserves MCP
stdio behavior when launched through pipes.

Bridge calls cannot dispatch back to the calling session or its recorded
ancestors. Delegation is limited to eight levels by default. After upgrading,
use **Connect bridge → Update the installed bridge skill** on each backend.
Older bridge sessions without recorded ancestry need fresh task sessions.

## License

Original content: [CC BY-NC 4.0](LICENSE); commercial use requires [separate
written permission](COMMERCIAL-LICENSE.md). Third-party dependencies keep their licenses.

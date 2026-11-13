# Codex Daemon Bridge

English | [简体中文](README.zh-CN.md)

Coordinate independent Codex App Server sessions across providers through MCP.
Discover available models, delegate tasks, and continue conversations on another provider.

## Prerequisites

- Linux with Python 3.11+ and the `codex` CLI (compatible with Codex 0.167.2).
- An existing Codex provider home: its `config.toml` declares `model_provider`, and credentials are already configured there.

## Install

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps .
source .venv/bin/activate
```

## Quick start

1. Create `~/.config/codexd-bridge/config.toml` (see the [example](examples/bridge.toml)):

   ```toml
   [backends.demo]
   home = "~/.codex"          # existing Codex home for this provider
   model_provider = "openai"  # exact provider ID declared in that home
   enabled = false           # connect enables the route after verification
   ```

2. If the backend daemon is not already running, start it and confirm readiness
   (the home's `config.toml` must declare the same `model_provider`):

   ```bash
   codexd-bridge daemon start demo
   codexd-bridge daemon status demo
   ```

3. Connect the running daemon and list its models. Connection includes a short
   model request to verify access.

   ```bash
   codexd-bridge connect demo
   codexd-bridge models list
   ```

   Add `--allow-session-writes` only to pre-approve the four session mutation
   tools for unattended delegation.

4. Add another provider under a different `[backends.NAME]` entry and repeat
   steps 2–3. Open a Codex session on a connected backend and ask it to delegate:

   > List the bridge backends and available models, then ask BACKEND_NAME to
   > review /absolute/path/to/project. Read its response when the task finishes.

Use `codexd-bridge --help` for more commands, including creating a new daemon home.

## License

Original content: [CC BY-NC 4.0](LICENSE); commercial use requires [separate
written permission](COMMERCIAL-LICENSE.md). Third-party dependencies keep their licenses.

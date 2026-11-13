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

## Create a provider

Export the API key in your shell, then supply the provider's endpoint, model ID,
and documented context limit (the values below are examples):

```bash
codexd-bridge provider add demo \
  --base-url https://provider.example/v1 --env-key DEMO_API_KEY \
  --model MODEL_ID --context-window 131072
codex-demo
```

This creates an independent home, verifies real inference, connects MCP, and
installs `codex-demo` in `~/.local/bin` (add that directory to `PATH`). Only the
environment variable name is saved. Existing homes and launchers are preserved.
Use `--wire-api chat` for Chat Completions, `--effort` for a supported reasoning
level, or `--model-catalog /absolute/catalog.json` for an existing native catalog.
Without a catalog, only the declared model is listed; automatic compaction is
set to 80% of its context limit.

If setup fails after creating the home, fix the reported issue and run
`codexd-bridge provider connect demo` to verify and install its launcher.
`codex-demo server start|status|stop` controls only that provider's daemon.

## Connect an existing provider

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

Bridge calls cannot dispatch back to the calling session or its recorded
ancestors. Delegation is limited to eight levels by default. After upgrading,
run `connect NAME --update-skill` on each backend to refresh its MCP tools;
older bridge sessions without recorded ancestry need fresh task sessions.

## License

Original content: [CC BY-NC 4.0](LICENSE); commercial use requires [separate
written permission](COMMERCIAL-LICENSE.md). Third-party dependencies keep their licenses.

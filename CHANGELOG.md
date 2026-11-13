# Changelog

## 0.4.1

- Add guided installation and a keyboard TUI covering provider setup, sessions, diagnostics and daemon management.
- Separate project uninstall from daemon removal, with explicit data deletion through `--purge`.
- Record session type, parent, task requirements and acceptance criteria for every nested delegation.
- Refresh owned MCP integrations during upgrades so new Codex sessions discover the updated tools.
- Preserve provider, model and reasoning settings when continuing or forking sessions.
- Fix message pagination and preserve JSON-looking text passed through MCP.
- Improve model probes, long-path connections, concurrent operations and failed setup recovery.
- Protect existing sessions, shared guides and user-edited files during cleanup and uninstall.

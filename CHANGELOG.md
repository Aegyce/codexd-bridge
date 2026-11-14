# Changelog

## Unreleased

- Distinguish backend model browsing from the cross-backend overview and disclose partial catalogs.
- Preserve model and session pages when returning from details and actions; retain pagination on empty pages with a next cursor.
- Honor deferred exit requests after returning from native Codex.
- Translate diagnostic results and native exit notices when switching the interface language.
- Retry transient directory-cleanup races during explicit daemon purge, rechecking the target and running service before each attempt.
- Add Chinese/English TUI switching, saved language preferences and shortcut help.
- Add numbered function areas, Vim-style navigation and contextual shortcut hints; Alt+1–9 focuses actions before Enter runs them.
- Align each card's title and description on one axis in both languages, preserving its center when focused.
- Provide matching Chinese and English README screenshots.
- Report Codex account and provider authentication separately, using live backend metadata.
- Let unconfigured users complete onboarding in native Codex and return with their bridge setup inputs preserved.
- Clarify ChatGPT browser/device-code login, OpenAI API key billing and enterprise access tokens.

## 0.4.1

- Add guided installation and a keyboard TUI covering provider setup, sessions, diagnostics and daemon management.
- Separate project uninstall from daemon removal, with explicit data deletion through `--purge`.
- Record session type, parent, task requirements and acceptance criteria for every nested delegation.
- Refresh owned MCP integrations during upgrades so new Codex sessions discover the updated tools.
- Preserve provider, model and reasoning settings when continuing or forking sessions.
- Fix message pagination and preserve JSON-looking text passed through MCP.
- Improve model probes, long-path connections, concurrent operations and failed setup recovery.
- Protect existing sessions, shared guides and user-edited files during cleanup and uninstall.

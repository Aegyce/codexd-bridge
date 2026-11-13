# Changelog

## 0.4.1

- Refresh owned, enabled MCP registrations after script upgrades, including recognized compatibility
  entries. Update unchanged bundled guides and preserve local edits without restarting daemons.
- Diagnose stale registration version markers and allow `provider connect --update-skill` to finish
  upgrades. Include a recovery hint when failed onboarding retained incorrect connection settings.
- Confirm a probe's initially interrupted snapshot before declaring failure; native startup can
  briefly report interruption before the live turn starts. Poll the same turn without restarting inference.
- Refuse to probe or delete a creation response that identifies existing session activity. Preserve
  the shared guide when removing an integration beside another installation's bridge entry.
- Reject non-regular delegation state files without blocking. Compare canonical session identities
  before failed-creation cleanup, protecting callers reached through home aliases.
- Preserve a newly created launcher if it was edited in place before registration rollback.
- Require the Responses API when creating providers; remove the unsupported Chat Completions
  choice. Exercise actual provider onboarding and removal against the local native fixture.
- Connect to native sockets in long and Unicode home paths through a directory descriptor;
  use the same addressing for daemon removal checks.
- Isolate shell-profile tests from the runner's configuration directories, and publish versioned
  GitHub Releases only after the full CI matrix and archive installation checks pass.

## 0.4.0 — release candidate

- Recheck provider launchers after model verification: restore a removed launcher and refuse a
  replaced one before enabling the route. Bind TUI stop confirmation to the displayed backend;
  changed registrations must be reviewed again before stopping a daemon.

- Preserve JSON-looking text and native pagination cursors through MCP argument validation.
  Exercise installed session operations against native daemons with a loopback model fixture.
- Validate endpoint hosts, ports and finite timeouts before provider creation. Share legacy MCP
  ownership detection between registration and removal; allow removal through home aliases with
  data retained. Serialize daemon removal with application installation and reject backend data
  inside the managed application directory before registration.
- Display native daemon results correctly in the TUI's custom-arguments action.

- Preserve unset reasoning effort when continuing and forking sessions. Accept catalog-advertised
  defaults even when the list of alternative reasoning levels is empty, and use the selected
  model's default in the verification form.
- Correct native launcher handling of approval/directory options, the `apply` alias, and prompt
  text following `--`; honor explicit `--no-daemon` without starting a background daemon.
  Preserve native delimiters through the public command parser as well as the launcher logic.
  Poll newly created probe histories while their first write is pending.
- Refuse linked directories, symbolic links and hard links when writing daemon failure logs,
  preserving the contents and permissions of unrelated files.

- Normalize installation paths through parent-directory links and `..`, so installed commands,
  integration ownership and uninstall all refer to the same location.
- Refuse to enable a route if its configuration changed during model verification. Prevent
  purging a backend nested inside another registered backend's directory.

- Add an independent user installation, upgrades that retain the working version on build failure,
  and `codexd-bridge uninstall`. Project removal retains native daemons and their data.
- Add `daemon remove NAME`, with optional `--purge` for explicit data deletion. Other backends
  remain available; incomplete removal retains a disabled route for retry.
- Expose all management operations through the keyboard TUI, including diagnostics and registry
  recovery. Provider creation uses one submission; installing a launcher works offline.
- Persist MCP registration and the bundled guide so fresh Codex sessions discover the bridge.
- Give every derived session a fresh assignment with its type, immediate parent, root ancestry,
  task requirements and acceptance criteria. Permit nested self-forks while rejecting delegation cycles.
- Preserve a resumed session's provider, model and reasoning effort. Serialize bridge continuation
  requests for one session and require the current turn ID for active-session steering.
- Continue partial messages without skipping the rest of a native page. Continuations carry text
  offsets and reject stale or mismatched history rather than returning unrelated content.
- Preserve concurrent shell-profile edits during installation, custom shell configuration locations,
  line endings and original final-newline state. Validate ownership records before uninstalling.
- Correct MCP context injection under Python 3.14; test Python 3.11 through 3.14 in CI.
- Add `doctor`, `--version`, source-to-wheel release checks, and repeatable isolated installation,
  upgrade, native discovery and removal checks.

The delegation-state format is upgraded in place. Older bridge versions cannot read the upgraded
state. Back up the registry and adjacent state files before upgrading an existing installation.
The license remains CC BY-NC 4.0, with separate written authorization for commercial use.

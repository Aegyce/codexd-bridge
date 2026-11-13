# Codex Daemon Bridge

[![English (current)](https://img.shields.io/badge/English-8C2F39?style=flat-square)](README.md) [![简体中文](https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-555555?style=flat-square)](README.zh-CN.md)

Coordinate independent Codex App Server sessions across providers through MCP.
Create provider launchers, discover models, and delegate tasks across independent daemons.

## Prerequisites

- Linux (including WSL), Python 3.11+ with `venv`, and the `codex` CLI.
- Provider access through an API key environment variable or an existing configured Codex home.

## Install

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
./install.sh
```

The installer creates an independent Python environment in your user directory,
installs the locked dependencies, adds the commands to your shell path, and opens
the TUI when run in a terminal. No `sudo` or virtual-environment activation is
needed. You can delete the downloaded project after installation. If the command
is not available in your current shell, open a new terminal; Bash, Zsh and Fish
startup files are configured automatically.

To upgrade, run `./install.sh` from the newer download. It refreshes this installation's
enabled MCP registrations and unmodified bundled guides without restarting daemons.
A failed build keeps the previous version available. For automated installation,
use `./install.sh --no-launch`.

## Open the manager

```bash
codexd-bridge
```

On the first screen, choose **Set up this Codex** to connect your existing
Codex account. Keep the suggested directory and name. Choose the configured
provider, or select **OpenAI** for standard Codex without an explicit provider
setting, then choose **Set up**. The interface starts the backend if needed,
checks model access, installs the guide and registers the tools. When setup
completes, new Codex sessions using that directory discover the bridge automatically.
Existing sessions may need to reconnect to refresh their tools.

For another service, choose **New provider** and follow the form. The service must
support the Responses API; the supported Codex version no longer accepts Chat Completions.
If the installer reports a customized guide or an incomplete refresh, open
**Set up this Codex** and choose **Update to this version** under the installed
guide options after reviewing your changes. Failed setup keeps your input and provides a retry action in the
interface. No separate registration command or manual configuration editing is
needed for these flows.

The manager also lets you browse models, manage sessions, start and stop daemons,
install a `codex-NAME` launcher, and open that provider's native Codex interface.

Use **Tab / Shift+Tab** to move, **Enter** to choose, **Ctrl+S** to submit,
**Esc** to go back, and **Ctrl+Q** to exit. All workflows support keyboard
navigation. The minimum terminal size is 60 columns by 18 rows.

New providers reference an API key environment variable already exported in the
terminal; enter its **name**, such as `DEMO_API_KEY`, in the form. The bridge
never saves the key value. Supply the provider's endpoint, model ID and documented
context limit. Select **Create provider** once to create the configuration, verify
a real response, enable the route, and install its launcher. If setup is incomplete,
use **Retry connection** to finish verification and integration.

**Install launcher** only installs the `codex-NAME` command; it does not start a
daemon or send a model request. The CLI equivalent is
`codexd-bridge provider launcher NAME`. **Connect bridge** verifies and integrates
the backend, retaining its selected guide-update and delegation-approval options
when retrying a failed connection.

The interface shows actual setup stages and keeps running while daemon requests
complete. **Open Codex** returns to the manager when the native interface closes.
Stopping a daemon requires confirmation. Unattended delegation is enabled only
when explicitly selected under **Connect bridge**.

For a different registry, run `codexd-bridge --config /absolute/bridge.toml ui`.
Command-line operations, including `install`, remain available for automation through `--help`.
MCP clients use `codexd-bridge serve`; the no-argument entry also preserves MCP
stdio behavior when launched through pipes.

Each created or forked task receives a fresh assignment containing its ID,
session type, parent and history source, root sessions, depth, task requirements,
and acceptance criteria. Set `session_type` (for example `worker` or `reviewer`)
and `acceptance_criteria`; `prompt` supplies the task requirements. Omitted criteria
are marked as unspecified. The record is included in the first message and
available through session reads and the manager. A child can fork itself again;
the new assignment identifies its immediate parent, while inherited assignments
remain historical context.

Bridge calls cannot dispatch back to the calling session or its recorded
ancestors. Forking the current session creates a new child. Delegation is limited
to eight levels by default. For installations managed outside `install.sh`, upgrade
the package and use **Connect bridge → Update the installed bridge skill** on each backend.
Older bridge sessions without recorded ancestry need fresh task sessions.
Existing recorded ancestry is retained when the state file is upgraded; this
state upgrade cannot be read by older bridge versions.

Repeated derivation and discovery are covered by automated tests. Discovery is
also checked with a fresh native thread in an isolated Codex home; this check
does not run model inference or establish every provider's model availability.

Common operations are available on the home screen. **More tools** provides daemon
configuration creation, registry selection, custom model verification, native Codex
arguments, installation diagnostics, MCP connection details, and bridge uninstallation. Session pages support
creating, continuing, forking, and steering tasks.

Run `codexd-bridge doctor` to check the installation and provider configuration without
making a model request. It prints JSON and exits with status 1 if a check fails.
Use `codexd-bridge --version` to identify the installed version.

## Remove one daemon

Select a backend and choose **Remove daemon**. By default, this stops its daemon
and removes its registration, bridge integration, and unchanged launcher. Configuration,
credentials, and sessions are retained for later **Attach existing**. Stopping interrupts
active tasks on that daemon.

```bash
codexd-bridge daemon remove NAME
```

To also delete all of that daemon's data, select deletion in the TUI and type its
name, or run:

```bash
codexd-bridge daemon remove NAME --purge
```

Deletion refuses the default or current Codex home, shared directories, and directories
containing the bridge executable, registry, or another backend. Failed removal retains
a disabled registration for retry. Other daemons and the bridge application are retained.

## Uninstall the bridge

```bash
codexd-bridge uninstall
```

You can also run `./uninstall.sh` from a downloaded copy. Both commands uninstall
**the Codex Daemon Bridge project itself**: its program, commands and shell-path
entries. They disable its registered MCP tools and remove an unchanged bundled
guide. Independent Codex daemons are neither uninstalled nor stopped; provider
configuration, credentials and sessions are retained.
Locally changed guides and unrelated commands are preserved.

Development and release checks: [release guide](docs/releasing.md),
[architecture](docs/architecture.md), and [changes](CHANGELOG.md).

## License

Original content: [CC BY-NC 4.0](LICENSE); commercial use requires [separate
written permission](COMMERCIAL-LICENSE.md). Third-party dependencies keep their licenses.

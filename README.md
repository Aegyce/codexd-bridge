# Codex Daemon Bridge

[![English (current)](https://img.shields.io/badge/English-8C2F39?style=flat-square)](README.md) [![简体中文](https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-555555?style=flat-square)](README.zh-CN.md)

Manage independent Codex daemons, discover provider models, and delegate tasks across providers through MCP.
A keyboard TUI guides setup and everyday operations.

![Character-based TUI home with Use backend and Manage backends cards](docs/images/tui-home.png)

*Running home screen: centered character cards expand slightly and invert their colors when focused.*

1. **Use backend:** choose a configured backend, then open Codex, inspect models or manage sessions.
2. **Manage backends:** add configurations, enable bridge tools and manage background services.

## Prerequisites

- Linux (including WSL), Python 3.11+ with `venv`, and the `codex` CLI.
- Codex can be unconfigured: use **Open native Codex** during setup to complete its own onboarding.

## Install

1. Download `codexd_bridge-VERSION.tar.gz` from [GitHub Releases](https://github.com/Aegyce/codexd-bridge/releases).
2. Extract it and run `./install.sh` from the extracted directory.
3. Follow the TUI opened by the installer. Later, run `codexd-bridge` from any directory.

The installer creates an independent Python environment and configures the command path for Bash, Zsh or Fish.
If the command is unavailable in your current terminal, open a new one. Installation needs no `sudo` or
virtual-environment activation; the downloaded directory can be deleted afterwards.

<details>
<summary>Install from a Git checkout</summary>

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
./install.sh
```

For automated installation, use `./install.sh --no-launch`.

</details>

## Set up Codex and connect the bridge

```bash
codexd-bridge
```

1. Choose **Manage backends → Set up this Codex**.
2. Keep the suggested directory and name, or choose another Codex home. If it is new or not signed in, select **Open native Codex**, complete Codex’s own setup and return here.
3. Use **Inspect Codex** to read the running backend’s provider, model and authentication state. A stopped backend is shown as unchecked.
4. Use its configured provider. For standard Codex without an explicit provider setting, choose **OpenAI**.
5. Choose **Set up**. The manager starts the backend if needed, verifies model access, installs the guide and registers MCP tools.

New Codex sessions using that home discover the bridge automatically. Existing sessions may need to reconnect.
Failed setup keeps your input and offers a retry action.

![Running TUI: setup form with a demonstration Codex directory](docs/images/tui-setup.png)

*Setup form captured from the running TUI with a demonstration directory.*

Fields appear from top to bottom; buttons follow their on-screen numbers:

| Field | Purpose |
| --- | --- |
| Codex directory | Selects the Codex configuration to connect. Defaults to `CODEX_HOME`, or `~/.codex` when unset; it refers to a configuration directory, not one chat. |
| Name in the manager | Sets its backend name. An existing route for the directory is reused. |
| Provider | Uses the provider already configured in the directory, or explicitly selects OpenAI for standard Codex. |
| Installed bridge guide | **Install if missing** preserves an existing guide; **Update to this version** replaces it, including local edits. |

| Button | Result |
| --- | --- |
| 1 · Set up | Starts the backend if needed, verifies a model response and enables bridge discovery for new sessions. |
| 2 · Inspect Codex | Reads provider, model and authentication metadata from a running backend; does not start it or make a model request. |
| 3 · Open native Codex | Opens Codex's own setup and login interface; returns with the bridge form inputs preserved. |
| 4 · Back | Returns to the management page. |

OpenAI sign-in is separate from the provider selection. Setup reuses the chosen Codex directory's login:

| Access | Sign-in method |
| --- | --- |
| ChatGPT plan access | Browser login (`codex login`) or device code (`codex login --device-auth`) |
| OpenAI Platform usage billing | OpenAI API key, managed by Codex |
| Enterprise automation | Codex access token, when enabled by the workspace administrator |

The status view distinguishes the authentication used by the provider from any saved Codex account.
It does not infer browser/device-code/access-token login from a ChatGPT account response.
Device code is a ChatGPT sign-in flow. See the [official authentication guide](https://learn.chatgpt.com/docs/auth) for requirements and login steps.

## Add another provider

Export the provider's API key environment variable before opening the manager, then choose **Manage backends → New provider**.
The service must support the Responses API.

| Form field | What to enter |
| --- | --- |
| Provider name | A short name such as `demo`; its launcher will be `codex-demo` |
| API endpoint | The provider's Responses API base URL |
| API key environment variable | The variable **name**, such as `DEMO_API_KEY`; the bridge does not save its value |
| Model ID | The exact model identifier supplied by the provider |
| Context limit (tokens) | The model's documented token limit |
| Reasoning effort (optional) | Leave blank to use the provider default |
| New Codex home (optional) | An absolute path for the independent configuration; blank uses `~/.local/share/codexd-bridge/homes/` |
| Launcher directory | Where to install `codex-NAME`; defaults to `~/.local/bin` |
| Existing model catalog (optional) | An absolute path to a model catalog file; blank declares only the entered model |

The bottom buttons are **1 · Create provider**, which creates the configuration, verifies a real response,
enables the route and installs its launcher, then **2 · Back**, which returns to management.
If setup is incomplete, choose **Retry connection**.

## Everyday operations

The following tables follow the interface: areas from left to right, then options from top to bottom.
Area numbers are selected with plain number keys; option numbers are selected with `Alt+number`, then `Enter`.

### 1. Use backend

Choose **Use backend** on the home screen. One configured backend opens directly; with several, choose a name and press `Enter`.
The example below shows the running interface with `qwen` selected.

![Running Use backend screen with work and navigation options](docs/images/tui-use.png)

| Area | Option | Result |
| --- | --- | --- |
| 1 · Work | 1 · Open Codex | Opens native Codex with the selected backend's configuration; returns to the manager when it closes. |
| 1 · Work | 2 · Account status | Shows the backend's provider, model, authentication and account metadata; does not verify model access or credits. |
| 1 · Work | 3 · Sessions | Lists this backend's sessions; supports creating, reading, continuing, forking and steering tasks. |
| 1 · Work | 4 · Models | Lists this backend's models; open a model's details to verify access or create a session. |
| 1 · Work | 5 · All models | Lists model catalogs across configured backends and any discovery errors. |
| 2 · Navigate | 1 · Switch backend | Returns to backend selection; shown when more than one backend is configured. |
| 2 · Navigate | 2 · Home | Returns to the two-card home screen; numbered 1 when Switch backend is absent. |

### 2. Manage backends

Choose **Manage backends** on the home screen. The read-only overview groups each backend's name, provider and bridge status,
with totals above. Short terminals show the totals; additional backends are counted when space is limited.

![Running management screen with setup, daemon and tools columns](docs/images/tui-manager.png)

| Area | Option | Result |
| --- | --- | --- |
| 1 · Setup | 1 · Set up this Codex | Opens the guided form above to enable bridge tools for a Codex configuration directory. |
| 1 · Setup | 2 · Current Codex | Inspects the default Codex directory's running backend, including when it has not been registered with the bridge. |
| 1 · Setup | 3 · New provider | Creates an independent provider configuration and launcher through the form above. |
| 1 · Setup | 4 · Attach existing | Adds an existing Codex directory to the registry; use Connect bridge to verify and enable it. |
| 1 · Setup | 5 · Connect bridge | Selects a registered backend, verifies model access and installs or refreshes its guide and MCP integration. |
| 1 · Setup | 6 · Install launcher | Installs `codex-NAME` for a registered backend, without starting a daemon or making a model request. |
| 2 · Daemon | 1 · Daemon status | Checks whether the selected backend's Codex service is running. |
| 2 · Daemon | 2 · Start daemon | Starts the selected backend's Codex service. |
| 2 · Daemon | 3 · Stop daemon | Stops the selected service after confirmation, interrupting its active tasks. |
| 2 · Daemon | 4 · Remove daemon | Stops and removes one backend; its data is retained unless deletion is explicitly selected. |
| 3 · Tools | 1 · More tools | Opens the additional actions listed below. |
| 3 · Tools | 2 · Refresh | Reloads the configured backend list and bridge status. |
| 3 · Tools | 3 · Home | Returns to the two-card home screen. |
| 3 · Tools | 4 · Quit | Closes the manager; running daemons remain active. |

With no configured backends, Connect bridge, Install launcher and the Daemon area are hidden; Tools becomes area 2.
Actions needing a target open backend selection when several are configured.

<details>
<summary>More tools, in button order</summary>

| Option | Result |
| --- | --- |
| 1 · Create daemon | Creates an independent configuration without starting a service or making a model request. |
| 2 · Choose registry | Opens another bridge registry by absolute path. |
| 3 · Diagnostics | Checks the installation and backend configuration without making model requests. |
| 4 · MCP connection | Shows the stdio command for connecting another MCP client. |
| 5 · Uninstall bridge | Uninstalls this application after confirmation; independent daemons and their data remain. |
| 6 · Verify model ID | Sends a short request using the entered model ID, then removes the temporary test session. |
| 7 · Codex arguments | Opens the selected backend's native Codex with additional arguments. |
| 8 · Back | Returns to management. |

Without configured backends, Verify model ID and Codex arguments are hidden, and Back becomes button 6.

</details>

### Keyboard controls

The interface follows your system language initially. Press `F2` to switch between English and Chinese;
your choice is saved. English text wraps at word boundaries, and Chinese uses terminal display widths.

| Key | Action |
| --- | --- |
| `?` / `F1` | Open shortcut help; use `F1` while typing in a field |
| `F2` | Switch English / 中文, preserving inputs and selection |
| `Tab` / `Shift+Tab` | Switch areas or fields; menus also accept `h` / `l` or `←` / `→` |
| `j` / `k` or `↓` / `↑` | Move within a menu or list |
| `1`–`9` | Focus a numbered home card or switch function area |
| `Alt+1`–`Alt+9` | Focus a numbered action in the current area or a bottom button; `Enter` runs it |
| `Home` / `End` | Select the first / last action in the area |
| `Ctrl+S` / `Esc` / `Ctrl+Q` | Submit form / go back / exit |

The minimum terminal size is 60 columns by 18 rows. Stopping a daemon requires confirmation;
unattended delegation is enabled only when explicitly selected under **Connect bridge**.

## Upgrade

- Run `./install.sh` from the newer download. It refreshes owned, enabled MCP registrations and unchanged bundled guides without restarting daemons. A failed build keeps the working version.
- If a guide has local edits or refresh is incomplete, review the changes, then use **Manage backends → Set up this Codex → Update to this version**.
- For other package-manager installations, update the package and use **Connect bridge → Update the installed bridge skill** on each backend.
- Recorded task ancestry survives state migration. Older bridge versions cannot read the upgraded state; old tasks without recorded ancestry need fresh sessions.

## Delegate tasks

- Every created or forked task gets a fresh assignment: ID, session type, parent and history source, root ancestry, depth, task requirements and acceptance criteria.
- Set `session_type` (such as `worker` or `reviewer`), `prompt` and `acceptance_criteria`. Omitted criteria are marked as unspecified. Assignments appear in the first message and in session reads.
- Children can fork again, recording their immediate parent. Delegation defaults to eight levels; dispatch back to the caller or its recorded ancestors is rejected. Steer an active task using its current turn ID.

## Remove a daemon or uninstall the project

| Action | Command | Effect |
| --- | --- | --- |
| Remove one daemon | `codexd-bridge daemon remove NAME` | Stops it and removes its registration, bridge integration and unchanged launcher; retains configuration, credentials and sessions |
| Remove one daemon and its data | `codexd-bridge daemon remove NAME --purge` | Also deletes that daemon's data |
| Uninstall the bridge | `codexd-bridge uninstall` | Removes the bridge program and owned integrations; retains independent daemons and their data |

- **TUI:** enter **Manage backends**, then choose **Remove daemon** or **More tools → Uninstall bridge**. Destructive actions ask for confirmation; data deletion also requires the backend name.
- Stopping or removing a daemon interrupts its active tasks. Retained data can be reconnected through **Attach existing**; a failed removal leaves a disabled registration for retry.
- Data deletion refuses protected or shared homes and directories containing the bridge, its registry or another backend.
- `./uninstall.sh` is an alternative for project removal. Uninstall preserves locally edited guides and unrelated commands.

## Command-line reference

| Task | Command |
| --- | --- |
| Diagnose installation and provider configuration | `codexd-bridge doctor` |
| Show the installed version | `codexd-bridge --version` |
| List automation commands | `codexd-bridge --help` |
| Open another registry | `codexd-bridge --config /absolute/bridge.toml ui` |
| Start the MCP stdio entry point | `codexd-bridge serve` |

Diagnostics make no model requests, print JSON and exit with status 1 on errors.
They are also available under **Manage backends → More tools → Diagnostics**.

See the [release guide](docs/releasing.md) for validation results and unverified platforms/models,
the [architecture](docs/architecture.md) for session and failure behavior, and the [changelog](CHANGELOG.md) for improvements.

## License

Original content: [CC BY-NC 4.0](LICENSE); commercial use requires [separate written permission](COMMERCIAL-LICENSE.md).
Third-party dependencies keep their licenses.

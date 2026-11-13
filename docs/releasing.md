# Release validation

The supported runtime is Linux, including WSL, with Python 3.11 or newer. The automated matrix
covers CPython 3.11, 3.12, 3.13 and 3.14. Native protocol checks currently use Codex CLI 0.167.2.
Windows without WSL, macOS, Linux ARM, other Codex versions and every provider/model combination
have not been validated. Do not infer support from a successful unit-test run.

## Reproduce the checks

From a checkout, create an isolated environment and install the locked runtime and check tools:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src tests scripts
bash -n install.sh uninstall.sh
git diff --check
.venv/bin/python scripts/build_release.py
.venv/bin/pip-audit --requirement requirements.lock --disable-pip --no-deps --format json --output tmp/dependency-audit.json
.venv/bin/python scripts/validate_install.py
```

The build creates `tmp/release-vVERSION/` with a source archive, a wheel built from that exact
archive, and `SHA256SUMS`. It checks entry points, version metadata, the bundled guide, installer
files and unwanted archive paths. `validate_install.py` runs the distributed installer twice in
a disposable user directory, checks the upgrade, deletes the download, and uninstalls through
the installed command from `/`. Failed validation directories are retained under `tmp/`.
The installed provider launcher is also exercised with a recording native CLI to verify argument
forwarding and the selected Codex home.

On a machine with Codex CLI, also run:

```bash
.venv/bin/python scripts/validate_install.py --native
```

This installs the actual archive and invokes the check using its installed Python environment.
It starts two disposable native daemons, verifies fresh-session tool and skill discovery before
and after a restart, removes and reattaches one backend, tests explicit purge, and uninstalls the
bridge while verifying the other daemon and its session still work. Only the daemons created by
the check are stopped. It also uses separate native processes and a loopback Responses fixture to
exercise the installed MCP entry point: nested forks and their assignment metadata, cross-provider
creation, explicit model changes, active-turn steering, probe cleanup and message pagination.
The installed `provider add` workflow is also exercised against that fixture, including a long
Codex home path, generated launcher, model verification, MCP discovery and explicit daemon purge.
No external model inference or real account configuration is used. Providers must expose the
Responses API; Codex CLI 0.167.2 rejects the former `wire_api="chat"` option. Live model
availability remains a separate provider-specific check through **Connect bridge** or `models`.

## CI and release gates

`.github/workflows/ci.yml` runs the matrix, lint and shell checks for pushes and pull requests.
Python 3.12 additionally audits runtime dependencies, builds and exercises the release archive,
then uploads the artifacts and audit report.
Actions are pinned to commit IDs; runtime and check-tool dependencies are hash locked.
Branch and pull-request checks use read-only repository permissions. A pushed `v*` tag also
runs a release job after the complete matrix passes. That job alone receives `contents: write`;
it verifies artifact checksums, uploads the source archive, wheel, checksums and dependency audit
to a draft, then publishes the release. Ordinary branch pushes do not publish releases.

Before publishing, require a passing matrix for the candidate commit, the native smoke above,
reviewed changes and version notes, and checked artifact hashes. Test the actual provider/model
combinations being advertised. Update this document when the supported matrix changes. A new
dependency or native Codex version requires these checks again.

GitHub Releases can distribute the source archive, wheel and checksum file. Users downloading
the source archive can extract it and execute `./install.sh`; they do not need to navigate into
the Python package. After explicit release authorization, push an annotated tag matching the
package version (for example, `v0.4.1`) to start publication. Verify the release job and downloadable
assets before announcing completion; a failed upload can leave an unpublished draft for recovery.

## Upgrades and support

Re-run the newer download's installer to upgrade the application. It refreshes owned, enabled
registrations through native MCP reload and updates guides that still match the previous bundle.
Stopped daemons use the new program on their next start; customized guides and incomplete refreshes
are reported for review through Connect bridge. Other package-manager installations must reconnect
their backends after updating the package. Existing session connections may require reconnecting;
the installer does not restart daemons. The native smoke also verifies registration refresh during
an actual upgrade and preservation of an existing session. The v2 delegation-state migration is forward-only; retain a backup of the registry and
adjacent state files if a downgrade may be needed. There is no automatic downgrade command.

For a useful bug report, include `codexd-bridge --version`, `codex --version`, Python/OS versions,
`codexd-bridge doctor`, the command or TUI action, expected behavior and the error. Review local
paths before sharing diagnostic output. Do not attach credentials or session histories.

The project's CC BY-NC 4.0 license and separate commercial-authorization requirement are
unchanged. Engineering validation does not change licensing terms.

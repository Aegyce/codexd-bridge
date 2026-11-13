#!/usr/bin/env bash
# Remove the bridge application; preserve independent Codex daemons and their data.
set -euo pipefail
project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${CODEXD_BRIDGE_PYTHON:-python3}" "$project_dir/scripts/manage_install.py" uninstall "$@"

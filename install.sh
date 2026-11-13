#!/usr/bin/env bash
set -euo pipefail
project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
installer_python="${CODEXD_BRIDGE_PYTHON:-python3}"
if ! command -v "$installer_python" >/dev/null 2>&1; then
    echo 'Python 3.11 or newer is required. Install Python, then run this script again.' >&2
    exit 1
fi
exec "$installer_python" "$project_dir/scripts/manage_install.py" install --source "$project_dir" "$@"

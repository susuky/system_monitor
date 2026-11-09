#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    printf '%s\n' '尚未安裝，請在專案目錄執行：' 'python3 -m venv .venv' '.venv/bin/python -m pip install -e .' >&2
    exit 1
fi

cd -- "$project_dir"
exec "$project_dir/.venv/bin/python" -m sysmon "$@"

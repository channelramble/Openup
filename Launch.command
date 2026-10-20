#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.9 or newer is required. Install it from https://www.python.org/downloads/macos/."
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is required.")'
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    python3 -m venv "$PROJECT_DIR/.venv"
fi
if ! "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/check_dependencies.py" 2>/dev/null; then
    echo "Installing the locked dependencies into this folder's virtual environment..."
    "$PROJECT_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --force-reinstall -r "$PROJECT_DIR/requirements-lock.txt"
    "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/check_dependencies.py"
fi
if [ "$#" -eq 0 ]; then
    if "$PROJECT_DIR/.venv/bin/python" -m ios_timeout wizard; then
        LAUNCH_STATUS=0
    else
        LAUNCH_STATUS=$?
    fi
    if [ -t 0 ]; then
        read -r -p "Press Return to close." _ || true
    fi
    exit "$LAUNCH_STATUS"
else
    exec "$PROJECT_DIR/.venv/bin/python" -m ios_timeout "$@"
fi

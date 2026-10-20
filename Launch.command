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
if ! "$PROJECT_DIR/.venv/bin/python" -c 'import paramiko; assert paramiko.__version__ == "5.1.0"' 2>/dev/null; then
    echo "Installing the pinned SSH dependency into this folder's virtual environment..."
    "$PROJECT_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements-lock.txt"
fi
if [ "$#" -eq 0 ]; then
    "$PROJECT_DIR/.venv/bin/python" -m ios_timeout wizard
    read -r -p "Press Return to close." _
else
    exec "$PROJECT_DIR/.venv/bin/python" -m ios_timeout "$@"
fi

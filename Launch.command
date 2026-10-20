#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.9 or newer is required. Install it from https://www.python.org/downloads/macos/."
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is required.")'
if [ "$#" -eq 0 ]; then
    if python3 "$PROJECT_DIR/bootstrap.py"; then
        LAUNCH_STATUS=0
    else
        LAUNCH_STATUS=$?
    fi
    if [ -t 0 ]; then
        read -r -p "Press Return to close." _ || true
    fi
    exit "$LAUNCH_STATUS"
else
    exec python3 "$PROJECT_DIR/bootstrap.py" "$@"
fi

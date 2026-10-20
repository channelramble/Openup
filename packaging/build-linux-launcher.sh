#!/bin/sh
set -eu
cd "$(dirname "$0")"
cc -Os -ffreestanding -fno-builtin -fno-ident -fno-stack-protector -fno-pie \
    -nostdlib -static -no-pie -Wl,--build-id=none,-z,noexecstack,-s \
    launcher-linux.c -o linux-launcher.bin
python3 - <<'PY'
import hashlib, json
from pathlib import Path
record = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
          for name in ("launcher-linux.c", "linux-launcher.bin")}
Path("linux-launcher.json").write_text(json.dumps(record, indent=2) + "\n")
PY

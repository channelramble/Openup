#!/usr/bin/env python3
"""Build the pinned iPhone 3G payload sources; requires a local iPhoneOS4.2 SDK.

SDK files are build inputs and are intentionally not redistributed here.
The linker must support ARMv6 (tested with cctools-port ld64-609).
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess

SOURCE_REVISION = "572dd5cd8c07f5f14f7ea9488041031dd22a26bb"
SOURCE_DATE_EPOCH = "1418119387"

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--linker", type=Path, required=True)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    src = root / "source" / "ramdisk_tools"
    sdk = args.sdk.resolve(strict=True)
    linker = args.linker.resolve(strict=True)
    out = (args.output or root / "payloads").resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    lines = (src / "Makefile").read_text().splitlines()
    manifest = {}
    for target in ("bruteforce", "device_infos", "restored_external"):
        sources = next(line.split(":", 1)[1].split() for line in lines if line.startswith(target + ":"))
        command = [args.clang, "-target", "armv6-apple-ios4.0", "-mcpu=arm1176jzf-s", "-marm", "-std=gnu99", "-Wall", "-Wno-pointer-sign", "-isysroot", str(sdk), '-DHGVERSION="' + SOURCE_REVISION[:12] + "+no-auto-fsck" + '"', "-O2", "-I.", "-I" + str(root / "include"), "-fuse-ld=" + str(linker), "-mlinker-version=609", "-Wl,-no_compact_unwind", "-Wl,-platform_version,ios,4.0,4.2", "-framework", "CoreFoundation", "-framework", "IOKit", "-framework", "Security", "-o", str(out / target), *sources]
        print("Building", target, flush=True)
        subprocess.run(command, cwd=src, env=env, check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", "--digest-algorithm=sha1,sha256", "--entitlements", str(src / "entitlements.plist"), "--timestamp=none", str(out / target)], check=True)
        subprocess.run(["codesign", "--verify", "--strict", "--verbose=2", str(out / target)], check=True)
        binary = out / target
        manifest[target] = {"sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "size": binary.stat().st_size}
    (out / "SHA256.json").write_text(json.dumps(manifest, indent=2) + "\n")

if __name__ == "__main__":
    main()

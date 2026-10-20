"""Validate the fixed iPhone 3G boot components around the pinned patcher."""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .core import ResetError, read_local_file, secure_write
from .host import reject_link

# iBSS comes from Apple's 3.1.3/7E18 IPSW: its original image reached Recovery
# on the tested 3G where 4.2.1 iBSS remained in DFU. The ramdisk/kernel and iBEC
# still come from 4.2.1/8C148. These are decoded component hashes, not firmware.
# The reviewed LukeZGD patcher
# returns 1 on success; the exact output hash is the acceptance criterion.
PROFILES = {
    "ibss": {
        "version": "3.1.3 / 7E18",
        "input": "d15e9b05fc6ddc995bf102de0ed4d7cdcd90374b2d80b7a98459edc041e41a3b",
        "output": "eba585529be94aa30fe560ea3fd2cd31caedb7d91d55d8eb759f43c82edc9fd0",
        "flags": ["--rsa", "--debug", "-b", "rd=md0 -v amfi=0xff cs_enforcement_disable=1"],
    },
    "ibec": {
        "version": "4.2.1 / 8C148",
        "input": "f67f77992b253f1d15a40d4c93985c5b20687db5d5e4fa2967ee9beb325ebf71",
        "output": "0210240d65585203edc7ee9fb718575e81cd5d202cf3048b929470b13a3a7902",
        "flags": ["--rsa", "--debug", "-b", "rd=md0 -v amfi=0xff cs_enforcement_disable=1"],
    },
}
MAX_COMPONENT = 2 * 1024 * 1024


def patch_component(kind, patcher, source, destination):
    if kind not in PROFILES:
        raise ResetError("Unknown iPhone 3G boot component.")
    profile = PROFILES[kind]
    source, destination = Path(source), Path(destination)
    if (source.resolve() == destination.resolve()
            or (destination.exists() and os.path.samefile(source, destination))):
        raise ResetError("Boot component output must be separate from its original input.")
    reject_link(destination)
    data = read_local_file(source, MAX_COMPONENT)
    if hashlib.sha256(data).hexdigest() != profile["input"]:
        raise ResetError(f"{kind} is not the original iPhone 3G {profile['version']} component.")
    with tempfile.TemporaryDirectory(prefix=".3g-boot-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "patched"
        result = subprocess.run(
            [str(Path(patcher).resolve()), str(source.resolve()), str(staged), *profile["flags"]],
            check=False, timeout=60,
        )
        if result.returncode not in (0, 1):
            raise ResetError(f"The boot component patcher failed with status {result.returncode}.")
        patched = read_local_file(staged, MAX_COMPONENT)
        if hashlib.sha256(patched).hexdigest() != profile["output"]:
            raise ResetError(f"The patched iPhone 3G {kind} did not match its verified checksum.")
        secure_write(destination, patched)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=sorted(PROFILES))
    parser.add_argument("patcher", type=Path)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        patch_component(args.component, args.patcher, args.input, args.output)
        print(f"Validated and patched iPhone 3G {PROFILES[args.component]['version']} {args.component}.")
        return 0
    except (ResetError, OSError, subprocess.SubprocessError) as exc:
        print(f"Boot component patch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

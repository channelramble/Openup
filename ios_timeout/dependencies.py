"""Check the complete dependency lock and native imports before launching."""

import importlib.metadata
import re
import sys
from importlib import resources
from pathlib import Path


def check(lockfile):
    return check_text(Path(lockfile).read_text())


def check_text(contents):
    problems = []
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9.+-]+", line):
            raise ValueError(f"Invalid dependency lock entry: {line}")
        name, required = line.split("==")
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{name} is missing")
            continue
        if actual != required:
            problems.append(f"{name}: installed {actual}, required {required}")
    if not problems:
        try:
            import paramiko  # noqa: F401
            import bcrypt  # noqa: F401
            import nacl.bindings  # noqa: F401
            from cryptography.hazmat.primitives.asymmetric import ed25519
            ed25519.Ed25519PrivateKey.generate()
        except Exception as exc:
            problems.append(f"SSH/native dependencies could not load: {exc}")
    return problems


def main():
    try:
        problems = check_text(resources.files("ios_timeout").joinpath("runtime-requirements.txt").read_text())
    except (OSError, ValueError) as exc:
        problems = [str(exc)]
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

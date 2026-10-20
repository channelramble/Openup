"""Build a reproducible source archive from an explicit file allowlist."""

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from ios_timeout import APP_NAME, __version__

ROOT = Path(__file__).resolve().parent
PREFIX = "Bruteforce-Legacy-iOS"


def provenance(root):
    result = {"project": APP_NAME, "version": __version__, "revision": None, "dirty": None}
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if Path(top).resolve() != root.resolve():
            return result
        result["revision"] = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        result["dirty"] = bool(subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def build(destination, root=ROOT):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / f"{PREFIX}-{__version__}.zip"
    names = [
        "README.md", "RESEARCH.md", "VERIFICATION.txt", "CHANGELOG.md", "LICENSE",
        "Launch.command", "Launch.sh", "Launch.cmd", "bootstrap.py", "pyproject.toml", "requirements-lock.txt",
        ".gitignore", ".gitattributes", "build_release.py", "check_dependencies.py",
        "desktop.py", "build_desktop.py", "desktop-build-lock.json", "macos_compatibility.py",
        "requirements-desktop.txt", "requirements-build.txt", "THIRD_PARTY.md",
    ]
    paths = [root / name for name in names]
    paths += sorted((root / "ios_timeout").glob("*.py"))
    paths += sorted((root / "ios_timeout").glob("*.txt"))
    paths += sorted((root / "ios_timeout").glob("*.svg"))
    paths += sorted(path for path in (root / "ios_timeout/data").rglob("*") if path.is_file())
    paths += sorted(path for path in (root / "packaging").rglob("*") if path.is_file())
    paths += sorted(path for path in (root / "third_party").rglob("*") if path.is_file())
    paths += sorted(path for path in (root / "docs").rglob("*") if path.is_file())
    paths += sorted((root / "tests").glob("*.py"))
    paths += sorted((root / ".github").rglob("*.yml"))
    entries = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Refusing non-regular release input: {path}")
        entries[path.relative_to(root).as_posix()] = path.read_bytes()
    entries["SOURCE.json"] = (json.dumps(provenance(root), indent=2) + "\n").encode()
    checksums = "".join(
        hashlib.sha256(data).hexdigest() + "  " + name + "\n"
        for name, data in sorted(entries.items())
    )
    entries["SHA256SUMS.txt"] = checksums.encode()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(f"{PREFIX}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name in ("Launch.command", "Launch.sh") else 0o100644) << 16
            archive.writestr(info, data)
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive CRC verification failed.")
        for name, data in entries.items():
            if archive.read(f"{PREFIX}/{name}") != data:
                raise ValueError(f"Archive contents differ: {name}")
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix(".zip.sha256").write_text(checksum + "  " + archive_path.name + "\n")
    return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    path = build(arguments.output)
    print(f"{path}\n{path.stat().st_size:,} bytes\n{path.with_suffix('.zip.sha256').read_text().strip()}")


if __name__ == "__main__":
    main()

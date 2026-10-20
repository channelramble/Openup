"""Build the source distribution without dependencies, backups, or device data."""

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DESTINATION = ROOT.parent / "release"
DESTINATION.mkdir(exist_ok=True)
ARCHIVE = DESTINATION / "iPhone-Timeout-Reset-1.0.0.zip"
names = [
    "README.md", "RESEARCH.md", "VERIFICATION.txt", "LICENSE",
    "Launch.command", "pyproject.toml", "requirements-lock.txt",
    ".gitignore", "build_release.py",
]
paths = [ROOT / name for name in names]
paths += sorted((ROOT / "ios_timeout").glob("*.py"))
paths += sorted((ROOT / "tests").glob("*.py"))
checksums = []
with zipfile.ZipFile(ARCHIVE, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in paths:
        name = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        checksums.append(hashlib.sha256(data).hexdigest() + "  " + name)
        archive.write(path, "iphone-timeout-reset/" + name)
    archive.writestr("iphone-timeout-reset/SHA256SUMS.txt", "\n".join(checksums) + "\n")

with zipfile.ZipFile(ARCHIVE) as archive:
    if archive.testzip() is not None:
        raise SystemExit("Archive CRC verification failed.")
    for line in checksums:
        expected, name = line.split("  ", 1)
        actual = hashlib.sha256(archive.read("iphone-timeout-reset/" + name)).hexdigest()
        if actual != expected:
            raise SystemExit("Archive content verification failed: " + name)
    launcher = archive.getinfo("iphone-timeout-reset/Launch.command")
    if not (launcher.external_attr >> 16) & 0o111:
        raise SystemExit("Launcher executable permission was not preserved.")

sha = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
ARCHIVE.with_suffix(".zip.sha256").write_text(sha + "  " + ARCHIVE.name + "\n")
print(f"{ARCHIVE}\n{ARCHIVE.stat().st_size:,} bytes\nSHA-256: {sha}")

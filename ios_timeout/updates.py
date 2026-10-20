"""Verified, staged updates from this project's GitHub releases."""

import hashlib
import io
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__
from .core import ResetError, read_local_file, secure_write
from .host import host_lock, state_directory, sync_directory

REPO = "channelramble/Bruteforce-Legacy-iOS"
PREFIX = "Bruteforce-Legacy-iOS/"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", value):
        raise ResetError("Invalid release version.")
    return tuple(map(int, value.split(".")))


def select_release(releases, prerelease=False):
    candidates = []
    for release in releases:
        tag = release.get("tag_name", "")
        if (release.get("draft") or (release.get("prerelease") and not prerelease)
                or not re.fullmatch(r"v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", tag)):
            continue
        version = tag[1:]
        candidates.append((version_tuple(version), release))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def get_releases():
    if not shutil.which("gh"):
        raise ResetError("Private-repository updates require GitHub CLI: install gh, then run gh auth login.")
    result = subprocess.run(["gh", "api", f"repos/{REPO}/releases?per_page=100"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ResetError("Cannot read releases. Run gh auth login with an account that can access this repository.")
    releases = json.loads(result.stdout)
    if not isinstance(releases, list):
        raise ResetError("GitHub returned an invalid release list.")
    return releases


def asset_for(release):
    version = release["tag_name"][1:]
    name = f"Bruteforce-Legacy-iOS-{version}.zip"
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(assets) != 1:
        raise ResetError(f"The release must contain exactly one {name} asset.")
    asset = assets[0]
    digest = asset.get("digest", "")
    if (not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
            or type(asset.get("id")) is not int or asset["id"] <= 0
            or type(asset.get("size")) is not int or not 0 < asset["size"] <= MAX_ARCHIVE):
        raise ResetError("Release asset has no valid GitHub digest, ID, or bounded size.")
    return asset


def archive_entries(path, version, expected_sha):
    data = read_local_file(path, MAX_ARCHIVE)
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise ResetError("Downloaded archive SHA-256 does not match GitHub.")
    entries = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            total = 0
            for info in archive.infolist():
                name = info.filename
                if not name.startswith(PREFIX):
                    raise ResetError("Unexpected archive root.")
                relative = name[len(PREFIX):]
                parts = PurePosixPath(relative).parts
                if (not parts or "\\" in relative or ":" in relative or ".." in parts
                        or relative.startswith("/") or str(PurePosixPath(relative)) != relative
                        or info.is_dir() or relative in entries
                        or stat.S_ISLNK(info.external_attr >> 16)
                        or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)):
                    raise ResetError("Unsafe or duplicate archive entry.")
                total += info.file_size
                if info.file_size > 16 * 1024 * 1024 or total > MAX_EXPANDED or len(entries) >= 2000:
                    raise ResetError("Expanded update exceeds the size limit.")
                entries[relative] = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise ResetError(f"Invalid update ZIP: {exc}") from exc
    verify_entries(entries, version)
    return entries


def verify_entries(entries, version):
    try:
        checksums = {}
        for line in entries["SHA256SUMS.txt"].decode("utf-8").splitlines():
            checksum, name = line.split("  ", 1)
            if name in checksums or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                raise ValueError("duplicate or invalid checksum")
            checksums[name] = checksum
        if set(checksums) != set(entries) - {"SHA256SUMS.txt"}:
            raise ValueError("checksum inventory does not match archive")
        for name, checksum in checksums.items():
            if hashlib.sha256(entries[name]).hexdigest() != checksum:
                raise ValueError(f"checksum mismatch: {name}")
        source = json.loads(entries["SOURCE.json"])
        if (source.get("version") != version or source.get("dirty") is not False
                or not re.fullmatch(r"[a-f0-9]{40}", source.get("revision", ""))):
            raise ValueError("source provenance does not match release")
        for name in ("bootstrap.py", "ios_timeout/__main__.py", "ios_timeout/launcher.py"):
            if not entries[name]:
                raise ValueError("missing launcher")
    except (KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise ResetError(f"Update verification failed: {exc}") from exc


def active_record(state):
    pointer = Path(state) / "updates/active.json"
    if not pointer.exists() and not pointer.is_symlink():
        return {"current": None, "previous": None}
    record = json.loads(read_local_file(pointer, 4096))
    for key in ("current", "previous"):
        if record.get(key) is not None:
            version_tuple(record[key])
    return record


def current_directory(state):
    record = active_record(state)
    version = record.get("current")
    if version is None:
        return None
    directory = Path(state) / "updates/versions" / version
    if directory.is_symlink() or not (directory / "bootstrap.py").is_file():
        raise ResetError("Active update is missing or invalid; use update --rollback.")
    # Detect corruption of installed source files before running the update.
    checksums = read_local_file(directory / "SHA256SUMS.txt", 1024 * 1024)
    entries = {"SHA256SUMS.txt": checksums}
    for line in checksums.decode().splitlines():
        _, name = line.split("  ", 1)
        parts = PurePosixPath(name).parts
        if not parts or ".." in parts or name.startswith("/") or "\\" in name or ":" in name:
            raise ResetError("Invalid installed-update path.")
        entries[name] = read_local_file(directory / name, 16 * 1024 * 1024)
    verify_entries(entries, version)
    return directory


def activate_archive(archive, state, version, digest):
    version_tuple(version)
    entries = archive_entries(archive, version, digest)  # Before any activation writes.
    directory = Path(state) / "updates"
    versions = directory / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    destination = versions / version
    if destination.is_symlink():
        raise ResetError("Installed version directory must not be a symlink.")
    if destination.exists():
        for name, contents in entries.items():
            if read_local_file(destination / name, 16 * 1024 * 1024) != contents:
                raise ResetError("Existing version differs from the verified release; it was not overwritten.")
    else:
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=versions) as temporary:
            staging = Path(temporary) / "app"
            staging.mkdir(mode=0o700)
            for name, contents in entries.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                secure_write(path, contents)
                if name in ("Launch.command", "Launch.sh"):
                    path.chmod(0o700)
            staging.rename(destination)
            sync_directory(versions)
    record = active_record(state)
    secure_write(directory / "active.json", json.dumps({
        "current": version, "previous": record.get("current"),
    }).encode())
    return destination


def run(args):
    state = args.state_dir.expanduser().absolute()
    with host_lock(state / "updates"):
        if args.rollback:
            record = active_record(state)
            if record.get("current") is None:
                print("No installed update is active.")
                return 0
            secure_write(state / "updates/active.json", json.dumps({
                "current": record.get("previous"), "previous": record.get("current"),
            }).encode())
            print("Previous version selected. Relaunch the app.")
            return 0
        release = select_release(get_releases(), args.prerelease)
        if release is None:
            print("No matching published release. Use --prerelease to include prereleases.")
            return 0
        version = release["tag_name"][1:]
        if version_tuple(version) <= version_tuple(__version__):
            print(f"Already current: {__version__}.")
            return 0
        asset = asset_for(release)
        print(f"Available: {version} (installed {__version__})")
        if not args.apply:
            print("Add --apply to download, verify, and activate this update.")
            return 0
        with tempfile.TemporaryDirectory(prefix=".download-", dir=state / "updates") as temporary:
            archive = Path(temporary) / "release.zip"
            with archive.open("wb") as output:
                result = subprocess.run(
                    ["gh", "api", f"repos/{REPO}/releases/assets/{asset['id']}",
                     "-H", "Accept: application/octet-stream"],
                    stdout=output, stderr=subprocess.PIPE, timeout=300,
                )
            if result.returncode or archive.stat().st_size != asset["size"]:
                raise ResetError("Update download failed or was incomplete. The current version is unchanged.")
            folder = activate_archive(archive, state, version, asset["digest"].split(":", 1)[1])
        print(f"Verified update installed: {folder}\nRelaunch the app. Backups and backend data are preserved.")
    return 0


def dispatch(arguments):
    # Keep rollback available even if an activated version is damaged.
    if arguments and arguments[0] == "update" and "--rollback" in arguments:
        return None
    directory = current_directory(state_directory())
    if directory and directory.resolve() != Path(__file__).resolve().parent.parent:
        return subprocess.run([sys.executable, str(directory / "bootstrap.py")] + arguments).returncode
    return None

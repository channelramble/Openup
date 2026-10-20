"""Native desktop updates: no pip, Git, or GitHub CLI on the user's computer."""

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__
from .core import ResetError, read_local_file, secure_write
from .host import host_lock, state_directory, sync_directory
from .updates import REPO, select_release, version_tuple

MAX_DOWNLOAD = 768 * 1024 * 1024
MAX_EXPANDED = 3 * 1024 * 1024 * 1024
TARGETS = {"macos-arm64", "macos-x86_64", "windows-x86_64", "linux-x86_64"}


def target():
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x86_64" if machine in {"amd64", "x86_64"} else machine
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    value = f"{system}-{architecture}"
    if value not in TARGETS:
        raise ResetError(f"No bundled desktop release is available for {value}.")
    return value


def bundle_name(platform_target):
    return "Bruteforce-Legacy-iOS"


def code_prefix(platform_target):
    return "Bruteforce Legacy iOS.app/Contents/Resources/app/" if platform_target.startswith("macos-") else ""


def python_relative(platform_target, windowed=False):
    name = ("pythonw.exe" if windowed else "python.exe") if platform_target.startswith("windows-") else "bin/python3"
    return code_prefix(platform_target) + "runtime/" + name


def required_files(platform_target):
    names = [code_prefix(platform_target) + "desktop.py",
             code_prefix(platform_target) + "ios_timeout/gui.py", python_relative(platform_target)]
    if platform_target.startswith("macos"):
        names += ["Bruteforce Legacy iOS.app/Contents/MacOS/BruteforceLegacyiOS",
                  "Bruteforce Legacy iOS.app/Contents/Info.plist"]
    elif platform_target.startswith("windows"):
        names += ["Bruteforce Legacy iOS.exe", "runtime/pythonw.exe", "wsl-runtime/bin/python3"]
    else:
        names += ["Bruteforce Legacy iOS", "Bruteforce Legacy iOS.sh"]
    return names


def find_bundle(source_root):
    for path in [Path(source_root), *Path(source_root).parents][:5]:
        if (path / "DESKTOP.json").is_file():
            return path
    return None


def safe_relative(name):
    if not isinstance(name, str):
        raise ResetError("Invalid package path.")
    parts = PurePosixPath(name).parts
    if (not parts or name.startswith("/") or "\\" in name or ":" in name or ".." in parts
            or str(PurePosixPath(name)) != name or "\x00" in name
            or any(part.endswith((" ", ".")) for part in parts)):
        raise ResetError("Unsafe package path.")
    return parts


def file_hash(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def validate_manifest(manifest, platform_target, version):
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or manifest.get("version") != version or manifest.get("target") != platform_target
            or manifest.get("dirty") is not False
            or not re.fullmatch(r"[a-f0-9]{40}", manifest.get("revision", ""))
            or not isinstance(manifest.get("files"), dict) or not 1 <= len(manifest["files"]) <= 30000):
        raise ResetError("Desktop package provenance does not match this release.")
    names = manifest["files"]
    folded = set()
    for name, item in names.items():
        safe_relative(name)
        if name.casefold() in folded or name == "DESKTOP.json":
            raise ResetError("Duplicate package path.")
        folded.add(name.casefold())
        if (not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", ""))
                or item.get("mode") not in (0o644, 0o755)):
            raise ResetError("Invalid desktop checksum inventory.")
    for required in required_files(platform_target):
        if required not in names:
            raise ResetError("The desktop package is missing its bundled runtime or GUI.")


def verify_bundle(root, platform_target, version, progress=lambda text: None):
    manifest = json.loads(read_local_file(Path(root) / "DESKTOP.json", 8 * 1024 * 1024))
    validate_manifest(manifest, platform_target, version)
    for index, (name, item) in enumerate(manifest["files"].items()):
        path = Path(root).joinpath(*safe_relative(name))
        if path.is_symlink() or not path.is_file() or file_hash(path) != item["sha256"]:
            raise ResetError("Installed desktop files changed. Open the original app and roll back the update.")
        if index % 200 == 0:
            progress("Verifying installed files…")
    return manifest


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host in {"api.github.com", "github.com", "release-assets.githubusercontent.com"}
            or host.endswith(".githubusercontent.com")
        ):
            raise ResetError("GitHub redirected outside the allowed HTTPS download hosts.")
        redirected = super().redirect_request(request, fp, code, message, headers, url)
        if redirected is not None and parsed.netloc != urllib.parse.urlsplit(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


def open_github(path, token="", binary=False):
    import certifi
    headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
               "User-Agent": "Bruteforce-Legacy-iOS-Desktop/" + __version__,
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        if any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ResetError("The GitHub token contains invalid characters.")
        headers["Authorization"] = "Bearer " + token
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())), SafeRedirect())
    try:
        return opener.open(urllib.request.Request(f"https://api.github.com/repos/{REPO}/{path}",
                                                 headers=headers), timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise ResetError("GitHub access failed. Use a token with read access to this private repository.") from None
        raise ResetError(f"GitHub returned HTTP {exc.code}. Try again later.") from None
    except urllib.error.URLError:
        raise ResetError("Could not connect securely to GitHub. Check the network and try again.") from None


def check(token="", prerelease=True, platform_target=None):
    platform_target = platform_target or target()
    with open_github("releases?per_page=100", token) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ResetError("Release metadata exceeds the size limit.")
    releases = json.loads(data)
    if not isinstance(releases, list):
        raise ResetError("GitHub returned invalid release metadata.")
    release = select_release(releases, prerelease)
    if release is None:
        return None
    version = release["tag_name"][1:]
    expected = f"Bruteforce-Legacy-iOS-{version}-{platform_target}.zip"
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == expected]
    if len(assets) != 1:
        if version_tuple(version) <= version_tuple(__version__):
            return None
        raise ResetError("The new release has no desktop package for this computer yet.")
    asset = assets[0]
    if (type(asset.get("size")) is not int or not 0 < asset["size"] <= MAX_DOWNLOAD
            or type(asset.get("id")) is not int or asset["id"] <= 0
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", asset.get("digest", ""))):
        raise ResetError("The desktop asset has no valid GitHub checksum or bounded size.")
    return {"version": version, "target": platform_target, "asset": asset,
            "url": f"https://github.com/{REPO}/releases/tag/v{version}"}


def read_pointer(state):
    path = Path(state) / "desktop-updates/active.json"
    if not path.exists():
        return {"current": None, "previous": None}
    data = json.loads(read_local_file(path, 4096))
    if not isinstance(data, dict):
        raise ResetError("Invalid desktop update record.")
    for value in (data.get("current"), data.get("previous")):
        if value is not None:
            version_tuple(value)
    return data


def installed_root(state, version, platform_target):
    return Path(state) / "desktop-updates/versions" / (version + "-" + platform_target) / bundle_name(platform_target)


def extract_archive(archive, destination, version, platform_target, checksum, progress=lambda text: None):
    if Path(archive).stat().st_size > MAX_DOWNLOAD or file_hash(archive) != checksum:
        raise ResetError("The desktop download does not match GitHub's SHA-256.")
    prefix = bundle_name(platform_target) + "/"
    with zipfile.ZipFile(archive) as package:
        members = {}
        total = 0
        for info in package.infolist():
            if not info.filename.startswith(prefix) or info.is_dir():
                raise ResetError("Unexpected desktop archive structure.")
            relative = info.filename[len(prefix):]
            safe_relative(relative)
            mode = info.external_attr >> 16
            if (relative in members or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or info.file_size > 256 * 1024 * 1024):
                raise ResetError("Unsafe or oversized desktop archive entry.")
            members[relative] = info
            total += info.file_size
            if total > MAX_EXPANDED or len(members) > 30001:
                raise ResetError("The desktop archive exceeds extraction limits.")
        if "DESKTOP.json" not in members or members["DESKTOP.json"].file_size > 8 * 1024 * 1024:
            raise ResetError("Missing desktop package inventory.")
        manifest = json.loads(package.read(members["DESKTOP.json"]))
        validate_manifest(manifest, platform_target, version)
        if set(members) != set(manifest["files"]) | {"DESKTOP.json"}:
            raise ResetError("Desktop inventory does not match the archive.")
        for index, (name, info) in enumerate(members.items()):
            path = Path(destination).joinpath(*safe_relative(name))
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            checksum = hashlib.sha256()
            with package.open(info) as source, path.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    checksum.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if name != "DESKTOP.json" and checksum.hexdigest() != manifest["files"][name]["sha256"]:
                raise ResetError("A desktop file failed checksum verification.")
            path.chmod(0o700 if name != "DESKTOP.json" and manifest["files"][name]["mode"] == 0o755 else 0o600)
            if index % 100 == 0:
                progress(f"Installing files… {index + 1} / {len(members)}")
    return manifest


def install(release, token="", state=None, progress=lambda text: None):
    state = Path(state or state_directory())
    version, platform_target = release["version"], release["target"]
    version_tuple(version)
    if platform_target not in TARGETS:
        raise ResetError("Unsupported desktop target.")
    area = state / "desktop-updates"
    with host_lock(area):
        destination = installed_root(state, version, platform_target)
        if destination.exists():
            verify_bundle(destination, platform_target, version, progress)
        else:
            destination.parent.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".stage-", dir=area) as temporary:
                archive = Path(temporary) / "desktop.zip"
                asset = release["asset"]
                received = 0
                with open_github(f"releases/assets/{asset['id']}", token, binary=True) as source, archive.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        received += len(chunk)
                        if received > asset["size"] or received > MAX_DOWNLOAD:
                            raise ResetError("Desktop download exceeds its declared size.")
                        output.write(chunk)
                        progress(f"Downloading… {received // (1024 * 1024)} / {(asset['size'] + 1048575) // 1048576} MB")
                if received != asset["size"]:
                    raise ResetError("The desktop download was interrupted.")
                staging = Path(temporary) / bundle_name(platform_target)
                extract_archive(archive, staging, version, platform_target, asset["digest"][7:], progress)
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(destination)
                sync_directory(destination.parent)
        progress("Activating the verified desktop update…")
        record = read_pointer(state)
        if record.get("current") != version:
            secure_write(area / "active.json", json.dumps({"current": version, "previous": record.get("current")}).encode())
    return destination


def rollback(state=None):
    state = Path(state or state_directory())
    with host_lock(state / "desktop-updates"):
        record = read_pointer(state)
        if record.get("current") is None:
            return False
        secure_write(state / "desktop-updates/active.json", json.dumps({
            "current": record.get("previous"), "previous": record.get("current")}).encode())
        return True


def dispatch(source_root, arguments):
    # The original app always opens its own recovery UI when an active bundle is damaged.
    if "--original" in arguments:
        return None
    state = state_directory()
    try:
        version = read_pointer(state).get("current")
        if version is None:
            return None
        platform_target = target()
        bundle = installed_root(state, version, platform_target)
        if bundle.resolve() == (find_bundle(source_root) or Path(source_root)).resolve():
            return None
        verify_bundle(bundle, platform_target, version)
        executable = bundle / python_relative(platform_target, windowed=True)
        script = bundle / code_prefix(platform_target) / "desktop.py"
        # Wait so a failed replacement process cannot silently orphan the launcher.
        return subprocess.run([str(executable), "-I", "-B", str(script), *arguments]).returncode
    except (ResetError, OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
        os.environ["BL_IOS_UPDATE_ERROR"] = "The installed update could not be verified. Open Updates and roll back."
        return None

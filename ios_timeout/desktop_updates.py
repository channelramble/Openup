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
from . import legacy_names
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
    return "Openup"


def code_prefix(platform_target, legacy=False):
    name = legacy_names.APP_NAME if legacy else "Openup"
    return name + ".app/Contents/Resources/app/" if platform_target.startswith("macos-") else ""


def python_relative(platform_target, windowed=False, legacy=False):
    name = ("pythonw.exe" if windowed else "python.exe") if platform_target.startswith("windows-") else "bin/python3"
    return code_prefix(platform_target, legacy) + "runtime/" + name


def required_files(platform_target, legacy=False):
    name = legacy_names.APP_NAME if legacy else "Openup"
    executable = legacy_names.MAC_EXECUTABLE if legacy else "Openup"
    names = [code_prefix(platform_target, legacy) + "desktop.py",
             code_prefix(platform_target, legacy) + "ios_timeout/gui.py", python_relative(platform_target, legacy=legacy)]
    if platform_target.startswith("macos"):
        names += [name + ".app/Contents/MacOS/" + executable,
                  name + ".app/Contents/Info.plist"]
    elif platform_target.startswith("windows"):
        names += [name + ".exe", "runtime/pythonw.exe", "wsl-runtime/bin/python3"]
    else:
        names += [name, name + ".sh"]
    return names


def legacy_layout(manifest, platform_target):
    names = manifest.get("files", {})
    return (all(name in names for name in required_files(platform_target, legacy=True))
            and not all(name in names for name in required_files(platform_target)))


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
    for required in required_files(platform_target, legacy_layout(manifest, platform_target)):
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
               "User-Agent": "Openup-Desktop/" + __version__,
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
        if exc.code == 401:
            raise ResetError("The optional GitHub token was rejected. Clear it in Advanced update options and try again.") from None
        if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
            raise ResetError("The update service's request limit was reached. Wait and try again later.") from None
        if exc.code == 403:
            raise ResetError("The update service denied this request. Clear any optional token and try again later.") from None
        if exc.code == 404:
            raise ResetError("This release or download is no longer available. Try Update now again to refresh the release.") from None
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
    expected = f"Openup-{version}-{platform_target}.zip"
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


def install_latest(token="", prerelease=True, state=None, progress=lambda text: None):
    """Find and stage the newest desktop release in one cancellable operation."""
    progress("Checking for the latest release…")
    release = check(token, prerelease)
    if release is None or version_tuple(release["version"]) <= version_tuple(__version__):
        progress("No newer release is available.")
        return None
    progress(f"Downloading version {release['version']}…")
    destination = install(release, token, state=state, progress=progress)
    return {"version": release["version"], "path": str(destination)}


def read_pointer(state):
    path = Path(state) / "desktop-updates/active.json"
    if not path.exists():
        return {"current": None, "previous": None}
    data = json.loads(read_local_file(path, 4096))
    if not isinstance(data, dict):
        raise ResetError("Invalid desktop update record.")
    for value in (data.get("current"), data.get("previous"), data.get("selected_by")):
        if value is not None:
            version_tuple(value)
    return data


def installed_root(state, version, platform_target):
    parent = Path(state) / "desktop-updates/versions" / (version + "-" + platform_target)
    current, previous = parent / bundle_name(platform_target), parent / legacy_names.SLUG
    return previous if previous.is_dir() and not current.exists() else current


def extract_archive(archive, destination, version, platform_target, checksum, progress=lambda text: None):
    if Path(archive).stat().st_size > MAX_DOWNLOAD or file_hash(archive) != checksum:
        raise ResetError("The desktop download does not match the release SHA-256.")
    prefix = bundle_name(platform_target) + "/"
    with zipfile.ZipFile(archive) as package:
        if package.namelist() and package.namelist()[0].startswith(legacy_names.SLUG + "/"):
            prefix = legacy_names.SLUG + "/"
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


def downloaded_release(archive, checksum_file, platform_target=None):
    """Inspect a user-selected release without credentials or network access."""
    archive = Path(archive)
    platform_target = platform_target or target()
    size = archive.stat().st_size
    if not 0 < size <= MAX_DOWNLOAD:
        raise ResetError("The desktop archive is empty or exceeds the size limit.")
    try:
        checksum_text = read_local_file(Path(checksum_file), 4096).decode("ascii").strip()
    except UnicodeError:
        raise ResetError("Choose the matching .zip.sha256 file from the release downloads.") from None
    match = re.fullmatch(r"([a-fA-F0-9]{64})(?:[ \t]+\*?([^\r\n]+))?", checksum_text)
    if not match:
        raise ResetError("Choose the matching .zip.sha256 file from the release downloads.")
    checksum = match[1].lower()
    if file_hash(archive) != checksum:
        raise ResetError("The selected ZIP does not match its SHA-256 file. Download both files again.")
    with zipfile.ZipFile(archive) as package:
        try:
            root_name = (bundle_name(platform_target) if bundle_name(platform_target) + "/DESKTOP.json" in package.namelist()
                         else legacy_names.SLUG)
            info = package.getinfo(root_name + "/DESKTOP.json")
        except KeyError:
            raise ResetError("Choose a desktop release ZIP for this computer, not the source archive.") from None
        if info.file_size > 8 * 1024 * 1024:
            raise ResetError("Desktop package inventory exceeds the size limit.")
        manifest = json.loads(package.read(info))
    if not isinstance(manifest, dict):
        raise ResetError("Invalid desktop package inventory.")
    version = manifest.get("version", "")
    version_tuple(version)
    validate_manifest(manifest, platform_target, version)
    expected = f"{root_name}-{version}-{platform_target}.zip"
    if match[2] is not None and match[2] != expected:
        raise ResetError("The checksum file names a different desktop release.")
    if version_tuple(version) <= version_tuple(__version__):
        raise ResetError(f"This app is already version {__version__}. Choose a newer release, or use Restore previous app version.")
    return {"version": version, "target": platform_target,
            "asset": {"size": size, "digest": "sha256:" + checksum}}


def copy_download(source, destination, size, progress, message):
    received = 0
    with destination.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            received += len(chunk)
            if received > size or received > MAX_DOWNLOAD:
                raise ResetError("Desktop download exceeds its declared size.")
            output.write(chunk)
            progress(f"{message}… {received // (1024 * 1024)} / {(size + 1048575) // 1048576} MB")
    if received != size:
        raise ResetError("The desktop download was interrupted.")


def install_downloaded(archive, checksum_file, state=None, progress=lambda text: None):
    progress("Checking the downloaded release…")
    release = downloaded_release(archive, checksum_file)

    def fetch(destination):
        with Path(archive).open("rb") as source:
            copy_download(source, destination, release["asset"]["size"], progress, "Copying update")

    # Recheck the staged copy too: the selected file could change during copying.
    destination = _install(release, fetch, state, progress)
    return {"version": release["version"], "path": str(destination)}


def install(release, token="", state=None, progress=lambda text: None):
    def fetch(destination):
        asset = release["asset"]
        with open_github(f"releases/assets/{asset['id']}", token, binary=True) as source:
            copy_download(source, destination, asset["size"], progress, "Downloading")

    return _install(release, fetch, state, progress)


def _install(release, fetch, state, progress):
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
                fetch(archive)
                staging = Path(temporary) / bundle_name(platform_target)
                extract_archive(archive, staging, version, platform_target, asset["digest"][7:], progress)
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(destination)
                sync_directory(destination.parent)
        progress("Activating the verified desktop update…")
        record = read_pointer(state)
        if record.get("current") != version:
            secure_write(area / "active.json", json.dumps({
                "current": version, "previous": record.get("current"),
                "selected_by": __version__}).encode())
    return destination


def rollback(state=None):
    state = Path(state or state_directory())
    with host_lock(state / "desktop-updates"):
        record = read_pointer(state)
        if record.get("current") is None:
            return False
        secure_write(state / "desktop-updates/active.json", json.dumps({
            "current": record.get("previous"), "previous": record.get("current"),
            "selected_by": __version__}).encode())
        return True


def dispatch(source_root, arguments):
    # The original app always opens its own recovery UI when an active bundle is damaged.
    if "--original" in arguments:
        return None
    state = state_directory()
    try:
        record = read_pointer(state)
        version = record.get("current")
        if version is None:
            return None
        platform_target = target()
        # A freshly downloaded Openup must not be silently replaced by an older
        # update selected by a previous installation. An explicit rollback from
        # this version (or newer) remains selected across subsequent launches.
        if (version_tuple(version) < version_tuple(__version__)
                and version_tuple(record.get("selected_by") or "0.0.0") < version_tuple(__version__)):
            return None
        bundle = installed_root(state, version, platform_target)
        if bundle.resolve() == (find_bundle(source_root) or Path(source_root)).resolve():
            return None
        manifest = verify_bundle(bundle, platform_target, version)
        legacy = legacy_layout(manifest, platform_target)
        executable = bundle / python_relative(platform_target, windowed=True, legacy=legacy)
        script = bundle / code_prefix(platform_target, legacy) / "desktop.py"
        # Wait so a failed replacement process cannot silently orphan the launcher.
        return subprocess.run([str(executable), "-I", "-B", str(script), *arguments]).returncode
    except (ResetError, OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
        os.environ["BL_IOS_UPDATE_ERROR"] = "The installed update could not be verified. Open Updates and roll back."
        return None

"""Build portable desktop distributions from hash-pinned Python runtimes and wheels.

This build tool is for maintainers. End users never invoke pip or install Python.
Cross-platform assembly uses each target's prebuilt runtime and extension wheels;
it does not claim to execute or validate foreign-platform binaries.
"""

import argparse
import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from ios_timeout import __version__
from ios_timeout.desktop_updates import bundle_name, code_prefix, file_hash, python_relative, safe_relative, validate_manifest
from build_release import provenance

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".desktop-cache"
LOCK = ROOT / "desktop-build-lock.json"
PYTHON_VERSION = "3.13.16"
PYTHON_RELEASE = "20261013"
TARGETS = {
    "macos-arm64": ("aarch64-apple-darwin", ["macosx_14_0_arm64", "macosx_14_0_universal2", "macosx_11_0_arm64"]),
    "windows-x86_64": ("x86_64-pc-windows-msvc", ["win_amd64"]),
    "linux-x86_64": ("x86_64-unknown-linux-gnu", ["manylinux_2_34_x86_64", "manylinux_2_28_x86_64",
                                               "manylinux_2_17_x86_64", "manylinux2014_x86_64"]),
}


def json_url(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "BL-iOS-build"}), timeout=60) as response:
        return json.load(response)


def requirements(target):
    values = [line.strip() for line in (ROOT / "requirements-lock.txt").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    values += ["PySide6-Essentials==6.12.0", "shiboken6==6.12.0", "certifi==2026.10.19"]
    if target.startswith("windows"):
        values.append("pywinpty==3.1.0")
    return values


def prepare_lock():
    CACHE.mkdir(exist_ok=True)
    metadata = json_url(f"https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{PYTHON_RELEASE}")
    assets = {asset["name"]: asset for asset in metadata["assets"]}
    previous = json.loads(LOCK.read_text()) if LOCK.exists() else {}
    locked = {"schema": 1, "python": PYTHON_VERSION, "python_release": PYTHON_RELEASE, "targets": {},
              "linux_libraries": previous.get("linux_libraries", [])}
    package_metadata = {}
    for target, (triple, tags) in TARGETS.items():
        directory = CACHE / "wheels" / target
        directory.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--python-version", "3.13",
                "--implementation", "cp", "--abi", "cp313", "--abi", "abi3", "--abi", "none",
                "--dest", str(directory)]
        for tag in tags:
            args += ["--platform", tag]
        subprocess.run(args + requirements(target), check=True)
        filename = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{triple}-install_only_stripped.tar.gz"
        runtime = assets[filename]
        if not runtime.get("digest", "").startswith("sha256:"):
            raise ValueError("Runtime has no GitHub checksum.")
        result = {"runtime": {"filename": filename, "url": runtime["browser_download_url"],
                              "sha256": runtime["digest"][7:], "size": runtime["size"]}, "wheels": []}
        for wheel in sorted(directory.glob("*.whl")):
            package, version = wheel.name.split("-")[:2]
            key = package + "/" + version
            if key not in package_metadata:
                package_metadata[key] = json_url("https://pypi.org/pypi/" + key + "/json")
            info = next(item for item in package_metadata[key]["urls"] if item["filename"] == wheel.name)
            digest = file_hash(wheel)
            if digest != info["digests"]["sha256"]:
                raise ValueError("Downloaded wheel differs from the PyPI checksum.")
            result["wheels"].append({"filename": wheel.name, "url": info["url"],
                                     "sha256": digest, "size": wheel.stat().st_size})
        locked["targets"][target] = result
    LOCK.write_text(json.dumps(locked, indent=2) + "\n")


def download(item, directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / item["filename"]
    if destination.is_file() and file_hash(destination) == item["sha256"]:
        return destination
    staged = destination.with_suffix(destination.suffix + ".partial")
    checksum = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(item["url"], timeout=60) as source, staged.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > item["size"]:
                    raise ValueError("Download exceeds locked size.")
                checksum.update(chunk)
                output.write(chunk)
        if size != item["size"] or checksum.hexdigest() != item["sha256"]:
            raise ValueError("Download differs from the build lock.")
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)
    return destination


def unpack_runtime(archive, destination):
    destination.mkdir()
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            if member.isdir():
                continue
            if not member.name.startswith("python/"):
                raise ValueError("Unexpected Python runtime archive.")
            relative = member.name[len("python/"):]
            # Runtime terminal descriptions have case-only aliases that form
            # loops on case-insensitive build hosts. The backends use the host's
            # terminfo; these data files are not needed by Python or the GUI.
            if (relative.startswith("share/terminfo/") or "__pycache__" in relative.split("/")
                    or relative.startswith("lib/python3.13/test/")):
                continue
            parts = safe_relative(relative)
            if not (member.isfile() or member.issym() or member.islnk()):
                raise ValueError("Unsupported runtime archive member.")
            path = destination.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            source = package.extractfile(member)  # Resolves links within the tar, never on the host.
            if source is None:
                raise ValueError("Missing runtime link target.")
            with source, path.open("wb") as output:
                shutil.copyfileobj(source, output)
            path.chmod(0o755 if member.mode & 0o111 or relative.startswith("bin/python") else 0o644)


def install_wheel(wheel, site_packages):
    with zipfile.ZipFile(wheel) as package:
        for item in package.infolist():
            if item.is_dir():
                continue
            name = item.filename
            parts = safe_relative(name)
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                    continue
                parts = parts[2:]
            destination = site_packages.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != package.read(item):
                    raise ValueError("Wheels contain conflicting files: " + name)
                continue
            destination.write_bytes(package.read(item))
            destination.chmod(0o755 if item.external_attr >> 16 & 0o111 else 0o644)


def install_linux_library(deb, destination, package_name):
    # Read Debian's ar container directly; never run package-maintainer scripts.
    data = Path(deb).read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("Invalid Linux runtime package.")
    offset = 8
    payload = None
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58])
        offset += 60
        if size < 0 or offset + size > len(data):
            raise ValueError("Truncated Linux runtime package.")
        if name.startswith("data.tar"):
            payload = data[offset:offset + size]
            if name.endswith(".zst"):
                import zstandard
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(payload)) as reader:
                    payload = reader.read(32 * 1024 * 1024 + 1)
                if len(payload) > 32 * 1024 * 1024:
                    raise ValueError("Oversized Linux library archive.")
            break
        offset += size + size % 2
    if payload is None:
        raise ValueError("Linux runtime package contains no data archive.")
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            if name.startswith("usr/lib/x86_64-linux-gnu/") and ".so" in Path(name).name and not member.isdir():
                path = destination / "platform-libs" / Path(name).name
            elif name == f"usr/share/doc/{package_name}/copyright":
                path = destination / "platform-libs/licenses" / (package_name + ".txt")
            else:
                continue
            if not (member.isfile() or member.issym() or member.islnk()):
                raise ValueError("Unsupported Linux library member.")
            path.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Missing Linux library data.")
            with source, path.open("wb") as output:
                shutil.copyfileobj(source, output)
            path.chmod(0o644)


def icons(directory):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    directory.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(str(ROOT / "ios_timeout/app-icon.svg"))
    for size in (16, 32, 64, 128, 256, 512, 1024):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        image.save(str(directory / f"icon-{size}.png"))
        if size == 256 and not image.save(str(directory / "app.ico"), "ICO"):
            raise ValueError("Could not create Windows icon.")
    if sys.platform == "darwin":
        iconset = directory / "App.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            shutil.copyfile(directory / f"icon-{size}.png", iconset / f"icon_{size}x{size}.png")
            shutil.copyfile(directory / f"icon-{size * 2}.png", iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(directory / "App.icns")], check=True)


def native_launcher(root, target, icon_directory):
    if target.startswith("windows"):
        compiler = shutil.which("x86_64-w64-mingw32-gcc") or shutil.which("gcc")
        resource_compiler = shutil.which("x86_64-w64-mingw32-windres") or shutil.which("windres")
        if not compiler or not resource_compiler:
            raise ValueError("Maintainer build requires MinGW-w64 gcc and windres.")
        resource = root.parent / "app.rc"
        resource.write_text('1 ICON "' + str(icon_directory / "app.ico").replace("\\", "/") + '"\n')
        object_file = root.parent / "app-icon.o"
        subprocess.run([resource_compiler, str(resource), "-O", "coff", "-o", str(object_file)], check=True)
        subprocess.run([compiler, "-Os", "-municode", "-mwindows", "-static-libgcc", "-Wl,--no-insert-timestamp",
                        str(ROOT / "packaging/launcher-windows.c"), str(object_file), "-o",
                        str(root / "Bruteforce Legacy iOS.exe")], check=True)
    elif target.startswith("macos"):
        directory = root / "Contents/MacOS"
        directory.mkdir(parents=True)
        arch = "arm64" if target.endswith("arm64") else "x86_64"
        subprocess.run(["clang", "-arch", arch, "-mmacosx-version-min=14.0", "-Os",
                        str(ROOT / "packaging/launcher-macos.c"), "-framework", "CoreFoundation",
                        "-o", str(directory / "BruteforceLegacyiOS")], check=True)
        shutil.copyfile(icon_directory / "App.icns", root / "Contents/Resources/App.icns")
        (root / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleName": "Bruteforce Legacy iOS", "CFBundleDisplayName": "Bruteforce Legacy iOS",
            "CFBundleIdentifier": "com.channelramble.bruteforcelegacyios",
            "CFBundleExecutable": "BruteforceLegacyiOS", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
            "CFBundleIconFile": "App.icns", "LSMinimumSystemVersion": "14.0",
            "NSHighResolutionCapable": True,
        }))
    else:
        compiled = ROOT / "packaging/linux-launcher.bin"
        inventory = json.loads((ROOT / "packaging/linux-launcher.json").read_text())
        for name in ("launcher-linux.c", "linux-launcher.bin"):
            if file_hash(ROOT / "packaging" / name) != inventory[name]:
                raise ValueError("Rebuild the Linux launcher after changing its source.")
        shutil.copyfile(compiled, root / "Bruteforce Legacy iOS")
        (root / "Bruteforce Legacy iOS").chmod(0o755)
        script = root / "Bruteforce Legacy iOS.sh"
        script.write_text('#!/bin/sh\nAPP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                          'exec "$APP_DIR/runtime/bin/python3" -I -B "$APP_DIR/desktop.py" "$@"\n')
        script.chmod(0o755)
        shutil.copyfile(icon_directory / "icon-256.png", root / "app-icon.png")


def build(target, *, allow_dirty=False):
    locked = json.loads(LOCK.read_text())["targets"][target]
    source = provenance(ROOT)
    if (source["dirty"] or not source["revision"]) and not allow_dirty:
        raise ValueError("Desktop releases require a clean committed source tree.")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    runtime_archive = download(locked["runtime"], CACHE / "runtimes")
    wheels = [download(item, CACHE / "wheels" / target) for item in locked["wheels"]]
    with tempfile.TemporaryDirectory(prefix="desktop-build-", dir=ROOT / "build") as temporary:
        root = Path(temporary) / bundle_name(target)
        code = root / code_prefix(target)
        code.mkdir(parents=True)
        print("Assembling " + target, flush=True)
        unpack_runtime(runtime_archive, code / "runtime")
        site = code / "runtime" / ("Lib/site-packages" if target.startswith("windows") else "lib/python3.13/site-packages")
        site.mkdir(parents=True, exist_ok=True)
        for wheel in wheels:
            install_wheel(wheel, site)
        if target.startswith("linux"):
            libraries = json.loads(LOCK.read_text()).get("linux_libraries")
            if not libraries:
                raise ValueError("The build lock is missing the Linux X11 library inventory.")
            for item in libraries:
                install_linux_library(download(item, CACHE / "linux-libraries"), code, item["package"])
        if target.startswith("windows"):
            # The Unix backend uses the included Linux runtime through WSL,
            # so no Python/venv/package installation is needed in the distribution.
            linux = json.loads(LOCK.read_text())["targets"]["linux-x86_64"]
            linux_archive = download(linux["runtime"], CACHE / "runtimes")
            unpack_runtime(linux_archive, code / "wsl-runtime")
            linux_site = code / "wsl-runtime/lib/python3.13/site-packages"
            linux_site.mkdir(parents=True, exist_ok=True)
            for item in linux["wheels"]:
                if item["filename"].startswith(("pyside6_", "shiboken6-")):
                    continue
                install_wheel(download(item, CACHE / "wheels/linux-x86_64"), linux_site)
        package = code / "ios_timeout"
        package.mkdir()
        for path in (ROOT / "ios_timeout").iterdir():
            if path.suffix in (".py", ".txt", ".svg"):
                shutil.copyfile(path, package / path.name)
        for name in ("desktop.py", "README.md", "LICENSE", "THIRD_PARTY.md", "VERIFICATION.txt", "CHANGELOG.md"):
            shutil.copyfile(ROOT / name, code / name)
        shutil.copyfile(LOCK, code / "desktop-build-lock.json")
        icon_directory = CACHE / "icons"
        icons(icon_directory)
        native_root = root / "Bruteforce Legacy iOS.app" if target.startswith("macos") else root
        native_launcher(native_root, target, icon_directory)
        if target.startswith("macos"):
            # Ad-hoc signing supports local execution; no Developer ID or notarization is claimed.
            subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(native_root)], check=True)
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(native_root)], check=True)
        manifest = {"schema": 1, "version": __version__, "target": target,
                    "revision": source["revision"], "dirty": source["dirty"], "files": {}}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                if path.is_symlink():
                    raise ValueError("Unexpected bundle symlink.")
                manifest["files"][path.relative_to(root).as_posix()] = {
                    "sha256": file_hash(path), "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644}
        (root / "DESKTOP.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        validate_manifest({**manifest, "dirty": False}, target, __version__)
        destination = output / f"Bruteforce-Legacy-iOS-{__version__}-{target}.zip"
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    info = zipfile.ZipInfo(root.name + "/" + path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with archive.open(info, "w", force_zip64=True) as stream, path.open("rb") as source_file:
                        shutil.copyfileobj(source_file, stream)
        destination.with_suffix(".zip.sha256").write_text(file_hash(destination) + "  " + destination.name + "\n")
        print(str(destination) + f" ({destination.stat().st_size:,} bytes)", flush=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-lock", action="store_true")
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true", help="Local development builds; updater rejects these.")
    args = parser.parse_args()
    if args.prepare_lock:
        prepare_lock()
    (ROOT / "build").mkdir(exist_ok=True)
    for target in TARGETS if args.all else [args.target] if args.target else []:
        build(target, allow_dirty=args.allow_dirty)


if __name__ == "__main__":
    main()

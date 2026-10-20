"""Verify and unpack the bundled iPhone 3G executables without running them."""

import hashlib
import io
import json
import stat
import zipfile
from importlib import resources
from pathlib import Path

from .backend_support import macho_architectures
from .core import ResetError, read_local_file

TOOLS = ("bruteforce", "device_infos", "restored_external")
MAX_FILE = 16 * 1024 * 1024


def _read_resource(path, maximum):
    if isinstance(path, Path):
        return read_local_file(path, maximum)
    # Source-edition WSL runs as a zipapp; its package resources are not paths
    # on the host filesystem. Keep the same read bound for those resources.
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ResetError("The bundled iPhone 3G resource exceeds its size limit.")
    return data


def read_bundled_payloads(data_directory=None):
    directory = Path(data_directory) if data_directory else resources.files("ios_timeout").joinpath("data")
    try:
        manifest = json.loads(_read_resource(directory.joinpath("iphone3g-armv6.json"), 64 * 1024))
        archive = _read_resource(directory.joinpath("iphone3g-armv6.zip"), MAX_FILE)
        if (manifest.get("schema") != 1 or manifest.get("product") != "iPhone1,2"
                or manifest.get("architecture") != "armv6"
                or manifest.get("ramdisk_build") != "8C148"
                or set(manifest.get("files", {})) != set(TOOLS)
                or hashlib.sha256(archive).hexdigest() != manifest.get("archive_sha256")):
            raise ResetError("The bundled iPhone 3G tools failed provenance verification.")
        values = {}
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            names = package.namelist()
            if len(names) != len(TOOLS) or set(names) != set(TOOLS):
                raise ResetError("The bundled iPhone 3G archive has unexpected entries.")
            for name in TOOLS:
                info = package.getinfo(name)
                expected = manifest["files"][name]
                mode = info.external_attr >> 16
                if (stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or not 28 <= info.file_size <= MAX_FILE
                        or info.file_size != expected.get("size")):
                    raise ResetError("Invalid bundled iPhone 3G executable: " + name)
                data = package.read(info)
                if hashlib.sha256(data).hexdigest() != expected.get("sha256"):
                    raise ResetError("Bundled iPhone 3G checksum failed: " + name)
                if 6 not in macho_architectures(data):
                    raise ResetError("Bundled iPhone 3G executable is not ARMv6: " + name)
                values[name] = data
        return manifest, values
    except ResetError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, zipfile.BadZipFile) as exc:
        raise ResetError(
            "Bundled iPhone 3G tools are missing or damaged. Extract the complete desktop download again."
        ) from exc


def extract_bundled_payloads(destination):
    """Create a new, private per-operation folder only after every file verifies."""
    _, values = read_bundled_payloads()
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    for name, data in values.items():
        path = destination / name
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o700)
    return destination

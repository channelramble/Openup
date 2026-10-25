"""Original-iPhone RAM helper and read-only legacy passcode recovery."""

import hashlib
import io
import json
import re
import stat
import zipfile
from argparse import Namespace
from importlib import resources
from pathlib import Path

from .backend_support import macho_architectures
from .core import ResetError, read_local_file

HELPER = "/usr/local/bin/openup-iphone2g"
KEYCHAIN = "/mnt2/Keychains/keychain-2.db"
MAX_HELPER = 1024 * 1024


def _resource(path, maximum):
    if isinstance(path, Path):
        return read_local_file(path, maximum)
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ResetError("The bundled iPhone 2G helper exceeds its size limit.")
    return value


def read_helper(data_directory=None):
    directory = Path(data_directory) if data_directory else resources.files("ios_timeout").joinpath("data")
    try:
        manifest = json.loads(_resource(directory.joinpath("iphone2g-helper.json"), 16384))
        archive = _resource(directory.joinpath("iphone2g-helper.zip"), MAX_HELPER)
        if (manifest.get("schema") != 1 or manifest.get("product") != "iPhone1,1"
                or manifest.get("architecture") != "armv6" or manifest.get("ramdisk_build") != "7E18"
                or manifest.get("minimum_ios") != "3.1"
                or hashlib.sha256(archive).hexdigest() != manifest.get("archive_sha256")):
            raise ValueError("manifest")
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            if package.namelist() != ["openup-iphone2g"]:
                raise ValueError("entries")
            info = package.getinfo("openup-iphone2g")
            if (not 28 <= info.file_size <= MAX_HELPER or info.file_size != manifest.get("size")
                    or stat.S_IFMT(info.external_attr >> 16) != stat.S_IFREG):
                raise ValueError("size/type")
            data = package.read(info)
        if hashlib.sha256(data).hexdigest() != manifest.get("sha256") or macho_architectures(data) != {6}:
            raise ValueError("binary")
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile) as exc:
        raise ResetError("The bundled iPhone 2G helper is missing or damaged. Reinstall the complete app.") from exc


def extract_helper(destination):
    data = read_helper()
    destination = Path(destination)
    with destination.open("xb") as stream:
        stream.write(data)
    destination.chmod(0o700)
    return destination


def probe_identity(device):
    try:
        value = device.command(HELPER + " --identity", timeout=15)
        if len(value) > 1024:
            raise ValueError("excessive identity")
        result = json.loads(value)
    except (ResetError, ValueError) as exc:
        raise ResetError("Cannot identify this 2G RAM disk. Use Prepare to boot Openup's iPhone 2G RAM disk.") from exc
    if (result != {"schema": 1, "product": "iPhone1,1", "board": "m68ap"}
            or type(result["schema"]) is not int):
        raise ResetError("The RAM disk did not verify original iPhone hardware.")
    return "iPhone1,1"


def recover_from_device(device, identity):
    from .legacy_keychain import recover_passcode
    identity.validate()
    if identity.model != "iPhone1,1" or probe_identity(device) != identity.model:
        raise ResetError("Legacy keychain recovery requires a verified original iPhone.")
    # Inspect must have mounted both volumes read-only. Recheck immediately
    # before obtaining a snapshot; never replay a journal or modify the phone.
    checked = device.inspect(expected_model="iPhone1,1", read_only=True)
    if checked != identity:
        raise ResetError("The connected phone changed during legacy recovery.")
    for suffix in ("-journal", "-wal", "-shm"):
        if device.exists(KEYCHAIN + suffix):
            raise ResetError("The legacy keychain has a pending journal. No recovery or repair was attempted.")
    database, _ = device.read(KEYCHAIN)
    key_hex = device.command(HELPER + " --key835", timeout=15).strip()
    if not re.fullmatch(r"[0-9a-f]{32}", key_hex) or key_hex in ("00" * 16, "01" * 16):
        raise ResetError("The RAM disk did not return a valid legacy hardware key.")
    return recover_passcode(database, bytes.fromhex(key_hex), installed_version=identity.version)


def run_recovery(args):
    from . import usbmux
    from .cli import launch_backend, ensure_kit
    from .device import USBDevice
    from .macos_agents import recovery_agents
    if args.armv6_tools:
        raise ResetError("Custom iPhone 3G recovery tools cannot run on the 2G. Clear the ARMv6 tools field.")
    if not args.apply and not args.install_only:
        print("iPhone 2G: experimental iPhone OS 2.2–3.1.3 recovery using its original keychain format.")
        print("Preview only. Add --apply to boot the 3.1.3 SSH RAM disk and read the passcode.")
        return 0
    read_helper()
    if args.install_only:
        kit = ensure_kit(args.backend_dir, args.state_dir, require_pinned=True)
        print(f"Pinned iPhone 2G ramdisk backend installed: {kit}")
        return 0
    prepare = Namespace(model="iPhone1,1", kit=args.backend_dir, state_dir=args.state_dir,
                        ipsw=None, udid=args.udid)
    print("iPhone 2G support is experimental; hardware validation is still pending.", flush=True)
    print("Recovery reads the existing passcode without changing the keychain or attempting codes.", flush=True)
    print("Recovery results appear in this session and in logs you explicitly export.", flush=True)
    with recovery_agents():
        ramdisk_serial = launch_backend(prepare)
        if not isinstance(ramdisk_serial, str) or not re.fullmatch(r"[0-9a-f]{40}", ramdisk_serial):
            raise ResetError("The 2G boot did not provide its expected USB session identity.")
        print("Waiting for the iPhone 2G SSH RAM disk…", flush=True)
        selected = usbmux.wait_for_device(ramdisk_serial, timeout=45)
        with USBDevice(selected, args.state_dir, "alpine") as device:
            identity = device.inspect(mount=True, expected_model="iPhone1,1", read_only=True)
            code = recover_from_device(device, identity)
            print("Legacy passcode: " + json.dumps(code, ensure_ascii=True), flush=True)
            print("Phone left in its ramdisk. Reboot using the ramdisk tool when ready.", flush=True)
    return 0

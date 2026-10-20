"""Read-only validation and bounded extraction of user-supplied IPSWs."""

import plistlib
import zipfile
from pathlib import Path

from .backend_support import PHONES
from .core import ResetError, secure_write
from .host import reject_link

THREE_G_BOOT_URL = (
    "https://secure-appldnld.apple.com/iPhone/061-7468.20100202.pbnrt/"
    "iPhone1,2_3.1.3_7E18_Restore.ipsw"
)


def seed_3g_url(kit, managed=False):
    """Avoid the metadata API for the fixed, verified 3G boot profile."""
    cache = Path(kit) / "saved/firmware/iPhone1,2/7E18/url"
    reject_link(cache)
    if managed or not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        secure_write(cache, (THREE_G_BOOT_URL + "\n").encode())


def validate_ipsw(path, model, build):
    if model not in PHONES:
        raise ResetError("Unknown IPSW target model.")
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("Restore.plist")
            if info.file_size > 1024 * 1024:
                raise ResetError("Oversized Restore.plist.")
            metadata = plistlib.loads(archive.read(info))
            if not isinstance(metadata, dict):
                raise ResetError("Invalid Restore.plist.")
            products = metadata.get("SupportedProductTypes", [])
            if not isinstance(products, list):
                raise ResetError("Invalid IPSW product list.")
            if metadata.get("ProductType"):
                products = list(products) + [metadata["ProductType"]]
            if model not in products or metadata.get("ProductBuildVersion") != build:
                raise ResetError(f"IPSW must be for {model} build {build}; installed iOS may differ.")
            member = f"Firmware/dfu/iBSS.{PHONES[model][0]}ap.RELEASE.dfu"
            ibss = archive.getinfo(member)
            if not 28 <= ibss.file_size <= 16 * 1024 * 1024:
                raise ResetError("Invalid iBSS size.")
            return member
    except (KeyError, zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
        raise ResetError(f"IPSW is incomplete or has the wrong boot image: {exc}") from exc


def extract_component(path, member, output):
    # Never extract paths from the archive onto the host.
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
        if not 0 < info.file_size <= 128 * 1024 * 1024:
            raise ResetError("Firmware component has an invalid size.")
        data = archive.read(info)  # Includes ZIP CRC validation.
    secure_write(Path(output), data)


if __name__ == "__main__":
    import sys
    try:
        extract_component(*sys.argv[1:])
    except Exception as exc:
        print(f"IPSW extraction failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

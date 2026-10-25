"""Strict, read-only physical inventory for legacy devices without an ECID.

This count is an extra connection gate, never a hardware identity check. Every
Apple mobile endpoint counts, including unknown models and missing serials.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from . import usb_modes
from .core import ResetError


# Normal range: libimobiledevice/usbmuxd src/usb.h. Boot modes: libirecovery.h.
# Apple T2 and Apple Silicon restore endpoints are not mobile phone endpoints.
MOBILE_PIDS = frozenset({0x1222, 0x1227, 0xF014, *range(0x1280, 0x1284), *range(0x1290, 0x12B0)})
SYSFS = Path("/sys/bus/usb/devices")
TIMEOUT = 2.0
MAX_NODES = 4096
MAX_DEPTH = 64


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ResetError("Physical USB inventory timed out; no firmware was uploaded.")
    return remaining


def _profiler_field(node, modern, legacy):
    if modern in node and legacy in node and node[modern] != node[legacy]:
        raise ValueError("Conflicting USB inventory fields")
    return node.get(modern, node.get(legacy))


def _profiler_devices(payload, data_type):
    if not isinstance(payload, bytes) or len(payload) > usb_modes.MAX_OUTPUT:
        raise ValueError("Missing or excessive USB inventory")
    document = json.loads(payload)
    if not isinstance(document, dict) or not isinstance(document.get(data_type), list):
        raise ValueError("Missing USB inventory schema")
    stack = [(node, str(index), 0) for index, node in enumerate(document[data_type])]
    devices, visited = [], 0
    while stack:
        node, location, depth = stack.pop()
        visited += 1
        if not isinstance(node, dict) or depth > MAX_DEPTH or visited > MAX_NODES:
            raise ValueError("Incomplete or excessive USB inventory")
        children = node.get("_items", [])
        if not isinstance(children, list):
            raise ValueError("Malformed USB inventory children")
        stack.extend((child, location + "/" + str(index), depth + 1)
                     for index, child in enumerate(children))
        vendor = _profiler_field(node, "USBDeviceKeyVendorID", "vendor_id")
        product = _profiler_field(node, "USBDeviceKeyProductID", "product_id")
        if vendor is None and product is None:
            if ("USBDeviceKeySerialNumber" in node or "serial_num" in node
                    or (depth > 0 and not children)):
                raise ValueError("USB endpoint has no vendor/product identity")
            continue  # A bus/container node, rather than a USB endpoint.
        vendor, product = usb_modes._usb_id(vendor), usb_modes._usb_id(product)
        if vendor is None or product is None:
            raise ValueError("Incomplete USB endpoint identity")
        if vendor == 0x05AC and product in MOBILE_PIDS:
            physical_location = _profiler_field(node, "USBKeyLocationID", "location_id")
            if not isinstance(physical_location, (str, int)) or isinstance(physical_location, bool):
                physical_location = "tree:" + location
            devices.append({"product_id": product, "location": str(physical_location)})
    return devices


def _mac_devices(deadline):
    for data_type in usb_modes.DATA_TYPES:
        remaining = _remaining(deadline)
        try:
            payload = usb_modes._query(data_type, remaining)
            _remaining(deadline)
            devices = _profiler_devices(payload, data_type)
            _remaining(deadline)
            return devices
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError, RecursionError):
            continue
    raise ResetError("Physical USB inventory is unavailable; no firmware was uploaded.")


def _sysfs_entries():
    entries = sorted(SYSFS.iterdir(), key=lambda entry: entry.name)
    if len(entries) > MAX_NODES:
        raise ValueError("Excessive USB inventory")
    devices = []
    for entry in entries:
        if re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*:[0-9]+\.[0-9]+", entry.name):
            continue  # USB interfaces share their parent physical device.
        if not re.fullmatch(r"(?:usb[0-9]+|[0-9]+-[0-9]+(?:\.[0-9]+)*)", entry.name) or not entry.is_dir():
            raise ValueError("Unrecognized USB inventory entry")
        devices.append(entry)
    return devices


def _sysfs_id(path):
    with path.open("rb") as stream:
        value = stream.read(17)
    if len(value) > 16:
        raise ValueError("Excessive USB endpoint identity")
    value = value.strip()
    if not re.fullmatch(rb"[0-9a-fA-F]{4}", value):
        raise ValueError("Malformed USB endpoint identity")
    return int(value, 16)


def _linux_devices(deadline):
    entries = _sysfs_entries()
    devices = []
    fingerprints = []
    for entry in entries:
        _remaining(deadline)
        identity = entry.stat()
        vendor, product = _sysfs_id(entry / "idVendor"), _sysfs_id(entry / "idProduct")
        fingerprints.append((entry.name, identity.st_dev, identity.st_ino, vendor, product))
        if vendor == 0x05AC and product in MOBILE_PIDS:
            devices.append({"product_id": product, "location": entry.name})
    if [entry.name for entry in _sysfs_entries()] != [entry.name for entry in entries]:
        raise ValueError("USB inventory changed while being read")
    for entry, expected in zip(entries, fingerprints):
        _remaining(deadline)
        identity = entry.stat()
        actual = (entry.name, identity.st_dev, identity.st_ino,
                  _sysfs_id(entry / "idVendor"), _sysfs_id(entry / "idProduct"))
        if actual != expected:
            raise ValueError("USB inventory changed while being read")
    _remaining(deadline)
    return devices


def require_single_mobile_device(*, deadline=None):
    """Require exactly one physical Apple mobile endpoint on macOS/Linux/WSL."""
    end = time.monotonic() + TIMEOUT
    deadline = end if deadline is None else min(deadline, end)
    _remaining(deadline)
    try:
        if sys.platform == "darwin":
            devices = _mac_devices(deadline)
        elif sys.platform.startswith("linux"):
            devices = _linux_devices(deadline)
        else:
            raise ResetError("Physical USB verification requires macOS or Linux/WSL; no firmware was uploaded.")
    except (OSError, ValueError, UnicodeError) as exc:
        raise ResetError("Physical USB inventory is incomplete or unavailable; no firmware was uploaded.") from exc
    if len(devices) != 1:
        raise ResetError(f"Connect exactly one Apple mobile device by USB; found {len(devices)}. No firmware was uploaded.")
    return devices[0]

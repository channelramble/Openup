"""Read-only macOS USB inventory for phones absent from usbmuxd in DFU/recovery.

These rows describe USB hardware, not an SSH endpoint or verified bootloader.
They never fabricate a usbmuxd UDID, and must not authorize firmware uploads.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

from .backend_support import PHONES


PROFILER = "/usr/sbin/system_profiler"
PROFILER_TIMEOUT = 5.0
MAX_OUTPUT = 1024 * 1024
DATA_TYPES = ("SPUSBHostDataType", "SPUSBDataType")
USB_MODES = {0x1227: "DFU", **dict.fromkeys(range(0x1280, 0x1284), "Recovery")}


def _query(data_type, timeout):
    """Bound memory, runtime and temporary output; never use backend utilities."""
    deadline = time.monotonic() + timeout
    # File-backed output avoids a full pipe blocking the process. Its size is
    # checked while running and again before any bytes are read into memory.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [PROFILER, data_type, "-json"], stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or os.fstat(output.fileno()).st_size > MAX_OUTPUT:
                    return None
                try:
                    process.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode or os.fstat(output.fileno()).st_size > MAX_OUTPUT:
                return None
            output.seek(0)
            return output.read(MAX_OUTPUT + 1)
        finally:
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=0.5)


def _usb_id(value):
    if type(value) is int:
        return value if 0 <= value <= 0xFFFF else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"0x([0-9a-fA-F]{1,4})(?: \([^\r\n]*\))?", value)
    return int(match[1], 16) if match else None


def _field(node, modern, legacy):
    # If both schemas appear, inconsistent values cannot identify a phone.
    if modern in node and legacy in node and node[modern] != node[legacy]:
        return None
    return node.get(modern, node.get(legacy))


def _identity(serial):
    if not isinstance(serial, str) or len(serial) > 4096:
        return None
    fields = {}
    for name, value in re.findall(r"(?<!\S)(CPID|BDID|ECID):[ \t]*([^\s]*)", serial, re.I):
        name = name.upper()
        maximum = {"CPID": 4, "BDID": 2, "ECID": 16}[name]
        if name in fields or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{1," + str(maximum) + "}", value):
            return None
        fields[name] = int(value, 16)
    if "CPID" not in fields or "BDID" not in fields:
        return None
    if "ECID" in fields and not fields["ECID"]:
        return None
    matches = [(model, board) for model, (board, chip, board_id) in PHONES.items()
               if (chip, board_id) == (fields["CPID"], fields["BDID"])]
    return (*matches[0], fields.get("ECID")) if len(matches) == 1 else None


def _parse(payload, data_type):
    """Return None for an unavailable schema, or rows from a valid inventory."""
    if not isinstance(payload, bytes) or len(payload) > MAX_OUTPUT:
        return None
    try:
        document = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get(data_type), list):
        return None
    rows = []
    stack = [(node, str(index)) for index, node in enumerate(document[data_type])]
    while stack:
        node, tree_path = stack.pop()
        if not isinstance(node, dict):
            continue
        children = node.get("_items", [])
        if isinstance(children, list):
            stack.extend((child, tree_path + "/" + str(index))
                         for index, child in enumerate(children))
        vendor = _usb_id(_field(node, "USBDeviceKeyVendorID", "vendor_id"))
        product = _usb_id(_field(node, "USBDeviceKeyProductID", "product_id"))
        if vendor != 0x05AC or product not in USB_MODES:
            continue
        identity = _identity(_field(node, "USBDeviceKeySerialNumber", "serial_num"))
        if identity is None:
            continue
        model, board, ecid = identity
        if ecid is not None:
            # Canonical numeric encoding survives capitalization/leading zeros
            # and mode/port changes without exposing the serial or raw ECID.
            key = "ecid:" + format(ecid, "x")
        else:
            location = _field(node, "USBKeyLocationID", "location_id")
            if not isinstance(location, (str, int)) or isinstance(location, bool):
                location = "tree:" + tree_path
            # Without ECID this only tracks a port, not a physical phone.
            key = "location:" + str(location) + ":" + model
        rows.append({"mode": USB_MODES[product], "model": model, "board": board + "ap",
                     "udid": "", "selection_id": "recovery:" +
                     hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]})
    return rows


def list_devices():
    """List recognized recovery USB phones, or [] when this probe is unavailable.

    Failure is deliberately local to this supplemental inventory: it must not
    hide normal-mode usbmuxd results. No recovery command or phone write occurs.
    """
    if sys.platform != "darwin":
        return []
    deadline = time.monotonic() + PROFILER_TIMEOUT
    for data_type in DATA_TYPES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            rows = _parse(_query(data_type, remaining), data_type)
        except (OSError, subprocess.SubprocessError):
            rows = None
        if rows is not None:
            return rows
    return []

"""Read-only, unpaired lockdownd information for the USB device selector.

These hints do not authorize recovery or replace its fresh hardware checks.
No pairing records, session requests, or recovery commands are sent here.
"""

import plistlib
import re
import struct
import time

from . import usbmux
from .backend_support import PHONES
from .core import ResetError


LOCKDOWN_PORT = 62078
MAX_REPLY = 64 * 1024
LENGTH = struct.Struct(">I")


def _exchange(sock, request, deadline):
    body = plistlib.dumps({"Label": "openup", **request})
    usbmux.set_deadline(sock, deadline)
    sock.sendall(LENGTH.pack(len(body)) + body)
    length, = LENGTH.unpack(usbmux.recv_exact(sock, LENGTH.size, deadline))
    if not 0 < length <= MAX_REPLY:
        raise ResetError("Invalid normal-device response length.")
    contents = usbmux.recv_exact(sock, length, deadline)
    try:
        reply = plistlib.loads(contents)
    except Exception as exc:
        raise ResetError("Invalid normal-device response.") from exc
    if not isinstance(reply, dict) or reply.get("Request") != request["Request"]:
        raise ResetError("Unexpected normal-device response.")
    if "Error" in reply or reply.get("Result", "Success") != "Success":
        raise ResetError("Device information is restricted or unavailable.")
    return reply


def device_info(device, *, path=None, deadline):
    with usbmux.connect_port(device, LOCKDOWN_PORT, path, deadline=deadline) as sock:
        reply = _exchange(sock, {"Request": "QueryType"}, deadline)
        if reply.get("Type") != "com.apple.mobile.lockdown":
            raise ResetError("Normal-device information is unavailable.")
        values = {}
        for field, key, pattern in (
            ("model", "ProductType", r"(?:iPhone|iPad|iPod|AppleTV)[0-9]+,[0-9]+"),
            ("board", "HardwareModel", r"[A-Za-z0-9]{2,32}"),
            ("version", "ProductVersion", r"[0-9]+(?:\.[0-9]+){0,3}"),
        ):
            value = _exchange(sock, {"Request": "GetValue", "Key": key}, deadline).get("Value")
            if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(pattern, value):
                raise ResetError("Invalid normal-device information.")
            values[field] = value.lower() if field == "board" else value
        if (values["model"] in PHONES
                and values["board"].removesuffix("ap") != PHONES[values["model"]][0]):
            raise ResetError("Normal-device product and board disagree.")
        return {**values, "mode": "normal"}


def enrich_devices(devices, *, path=None, timeout=6.0, device_timeout=2.0):
    """Keep every USB row even when locked, untrusted, absent, or timed out."""
    deadline = time.monotonic() + max(0.0, timeout)
    enriched = []
    for device in devices:
        row = dict(device)
        now = time.monotonic()
        if now >= deadline:
            row["info_error"] = "Device information was not read before the scan deadline."
        else:
            try:
                row.update(device_info(device, path=path,
                                       deadline=min(deadline, now + max(0.0, device_timeout))))
            except (ResetError, OSError, ValueError):
                row["info_error"] = "Device information is unavailable; this USB device is still listed."
        enriched.append(row)
    return enriched

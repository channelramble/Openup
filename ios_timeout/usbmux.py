"""Minimal local usbmuxd plist client; no TCP listener or iproxy is needed."""

import plistlib
import re
import socket
import struct
import sys

from .core import ResetError
from . import __version__

SOCKET_PATH = "/var/run/usbmuxd"
HEADER = struct.Struct("<IIII")
MAX_PACKET = 4 * 1024 * 1024


def recv_exact(sock, size):
    chunks = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise ResetError("USB connection closed unexpectedly.")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def request(sock, message, tag=1):
    payload = {
        "ClientVersionString": f"bruteforce-legacy-ios/{__version__}",
        "ProgName": "bruteforce-legacy-ios", "kLibUSBMuxVersion": 3,
        **message,
    }
    body = plistlib.dumps(payload)
    sock.sendall(HEADER.pack(HEADER.size + len(body), 1, 8, tag) + body)
    length, version, kind, response_tag = HEADER.unpack(recv_exact(sock, HEADER.size))
    if not HEADER.size <= length <= MAX_PACKET or version != 1 or kind != 8 or response_tag != tag:
        raise ResetError("Invalid usbmuxd response header.")
    try:
        reply = plistlib.loads(recv_exact(sock, length - HEADER.size))
    except Exception as exc:
        raise ResetError("Invalid usbmuxd plist response.") from exc
    if not isinstance(reply, dict):
        raise ResetError("Invalid usbmuxd response.")
    return reply


def default_endpoint():
    return ("127.0.0.1", 27015) if sys.platform == "win32" else SOCKET_PATH


def connect_socket(path=None):
    path = default_endpoint() if path is None else path
    sock = socket.socket(socket.AF_INET if isinstance(path, tuple) else socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(15)
    try:
        sock.connect(path)
    except OSError as exc:
        sock.close()
        raise ResetError(
            f"Cannot contact usbmuxd at {path}: {exc}. "
            "Use the computer connected to the phone. On Windows, install/start "
            "Apple Mobile Device Service; on Linux, install/start usbmuxd."
        ) from exc
    return sock


def list_devices(path=None):
    with connect_socket(path) as sock:
        reply = request(sock, {"MessageType": "ListDevices"})
    devices = reply.get("DeviceList")
    if not isinstance(devices, list):
        raise ResetError("usbmuxd did not return a device list.")
    usb = []
    seen_ids, seen_udids = set(), set()
    for device in devices:
        if not isinstance(device, dict):
            raise ResetError("usbmuxd returned a malformed device entry.")
        properties = device.get("Properties", {})
        if not isinstance(properties, dict):
            raise ResetError("usbmuxd returned malformed device properties.")
        if properties.get("ConnectionType") == "USB":
            identifier = properties.get("SerialNumber")
            device_id = device.get("DeviceID")
            if (not isinstance(identifier, str)
                    or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", identifier)
                    or type(device_id) is not int or not 0 < device_id < 2**32):
                raise ResetError("usbmuxd returned an invalid USB device identity.")
            if device_id in seen_ids or identifier in seen_udids:
                raise ResetError("usbmuxd returned ambiguous duplicate USB identities.")
            seen_ids.add(device_id)
            seen_udids.add(identifier)
            usb.append({"udid": identifier, "device_id": device_id,
                        "product_id": properties.get("ProductID")})
    return usb


def select_device(devices, udid=None):
    matches = [d for d in devices if udid is None or d["udid"] == udid]
    if not matches:
        raise ResetError(
            "No matching USB iPhone is exposed by usbmuxd. "
            "DFU alone is not enough: boot an SSH ramdisk first."
        )
    if len(matches) != 1:
        raise ResetError("Several USB devices are connected. Select one with --udid.")
    return matches[0]


def tunnel(device_id, port=22, path=None, expected_udid=None):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ResetError("SSH device port must be between 1 and 65535.")
    if type(device_id) is not int or not 0 < device_id < 2**32:
        raise ResetError("Invalid USB connection identifier.")

    def verify_selection():
        if expected_udid is not None:
            current = select_device(list_devices(path), expected_udid)
            if current["device_id"] != device_id:
                raise ResetError("USB device changed during connection. Re-run device selection.")

    verify_selection()
    sock = connect_socket(path)
    try:
        reply = request(sock, {
            "MessageType": "Connect", "DeviceID": device_id,
            "PortNumber": socket.htons(port),
        })
        if type(reply.get("Number")) is not int or reply["Number"] != 0:
            raise ResetError(
                f"Cannot connect to ramdisk SSH port {port} "
                f"(usbmuxd result {reply.get('Number')!r}). Boot an SSH ramdisk first."
            )
        verify_selection()
        return sock
    except BaseException:
        sock.close()
        raise

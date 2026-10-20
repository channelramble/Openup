"""Minimal local usbmuxd plist client; no TCP listener or iproxy is needed."""

import plistlib
import socket
import struct

from .core import ResetError

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
        "ClientVersionString": "iphone-timeout-reset/1.0.0",
        "ProgName": "iphone-timeout-reset", "kLibUSBMuxVersion": 3,
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


def connect_socket(path=SOCKET_PATH):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(15)
    try:
        sock.connect(path)
    except OSError as exc:
        sock.close()
        raise ResetError(
            f"Cannot contact usbmuxd at {path}: {exc}. "
            "Run on the Mac physically connected to the phone."
        ) from exc
    return sock


def list_devices(path=SOCKET_PATH):
    with connect_socket(path) as sock:
        reply = request(sock, {"MessageType": "ListDevices"})
    devices = reply.get("DeviceList")
    if not isinstance(devices, list):
        raise ResetError("usbmuxd did not return a device list.")
    usb = []
    for device in devices:
        properties = device.get("Properties", {})
        if properties.get("ConnectionType") == "USB":
            identifier = properties.get("SerialNumber")
            if isinstance(identifier, str) and isinstance(device.get("DeviceID"), int):
                usb.append({"udid": identifier, "device_id": device["DeviceID"],
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


def tunnel(device_id, port=22, path=SOCKET_PATH):
    if not 1 <= port <= 65535:
        raise ResetError("SSH device port must be between 1 and 65535.")
    sock = connect_socket(path)
    try:
        reply = request(sock, {
            "MessageType": "Connect", "DeviceID": device_id,
            "PortNumber": socket.htons(port),
        })
        if reply.get("Number") != 0:
            raise ResetError(
                f"Cannot connect to ramdisk SSH port {port} "
                f"(usbmuxd result {reply.get('Number')!r}). Boot an SSH ramdisk first."
            )
        return sock
    except BaseException:
        sock.close()
        raise

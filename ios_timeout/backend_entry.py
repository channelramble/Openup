"""Checked normal-mode entry shared by the two pinned ramdisk backends."""

import argparse
import math
import os
import plistlib
import re
import signal
import tempfile
import subprocess
import sys
import time
from xml.parsers.expat import ExpatError

from .backend_support import PHONES, resolve_identity
from .core import ResetError
from .desktop_protocol import emit_event
from . import usbmux


class NoNormalDevice(Exception):
    pass


class UnreadableNormalDevice(Exception):
    pass


class NormalServiceUnavailable(ResetError):
    pass


def _progress(message, remaining=None):
    # Command substitution reserves stdout for the verified identity. CLI
    # callers still need the same button instructions and timer as the GUI.
    print(message + (f" ({remaining} seconds remaining)" if remaining is not None else ""),
          file=sys.stderr, flush=True)
    emit_event({"type": "progress", "title": "Prepare your iPhone", "message": message,
                "remaining": remaining}, stream=sys.stderr)


def _continue(message):
    print(message, file=sys.stderr, flush=True)
    emit_event({"type": "prompt", "kind": "continue", "title": "Prepare your iPhone",
                "message": message, "options": [], "secret": False}, stream=sys.stderr)
    if not sys.stdin.readline():
        raise ResetError("Device preparation was cancelled; no firmware was uploaded.")


def _devices(deadline=None):
    deadline = min(deadline, time.monotonic() + 2.0) if deadline is not None else time.monotonic() + 2.0
    try:
        return usbmux.list_devices(deadline=deadline)
    except OSError as exc:
        raise NormalServiceUnavailable("The normal-mode USB service is unavailable.") from exc
    except ResetError as exc:
        if isinstance(exc.__cause__, OSError):
            raise NormalServiceUnavailable("The normal-mode USB service is unavailable.") from exc
        raise


def _select_normal(udid="", *, required=True, deadline=None):
    devices = _devices(deadline=deadline)
    if len(devices) > 1:
        raise ResetError("Connect only the selected iPhone before starting recovery. No device was rebooted.")
    if not devices:
        if required:
            raise NoNormalDevice()
        return None
    device = devices[0]
    if udid and device["udid"] != udid:
        raise ResetError("The connected USB phone does not match the selected USB serial. No device was rebooted.")
    return device


def _ecid(value, base=10, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError("Invalid ECID")
    if isinstance(value, str):
        value = int(value, base)
    if type(value) is not int or not (0 if allow_zero else 1) <= value < 2**64:
        raise ValueError("Invalid ECID")
    return str(value)


def _normal_identity(values, udid, expected):
    product, board = values.get("ProductType"), values.get("HardwareModel")
    if not isinstance(product, str) or not isinstance(board, str) or not product or not board:
        raise UnreadableNormalDevice()
    product, board = resolve_identity(product, board, expected)
    reported_udid = values.get("UniqueDeviceID")
    if reported_udid and reported_udid != udid:
        raise ResetError("Normal-mode identity disagrees with the selected USB serial. No device was rebooted.")
    try:
        ecid = _ecid(values.get("UniqueChipID"), allow_zero=expected == "iPhone1,1")
    except (TypeError, ValueError):
        raise UnreadableNormalDevice() from None
    version = values.get("ProductVersion", "Unknown")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", version):
        version = "Unknown"
    return ("Normal", product, board, ecid, version, udid)


def _run_info(command, *, timeout):
    """Bound time and output while retaining interrupt cleanup for native tools."""
    deadline = time.monotonic() + timeout
    owns_group = os.name != "nt"
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.DEVNULL, start_new_session=owns_group)
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 1024 * 1024:
                    return None
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 1024 * 1024:
                return None
            output.seek(0)
            return subprocess.CompletedProcess(command, process.returncode, output.read(1024 * 1024 + 1))
        finally:
            if process.poll() is None:
                try:
                    if owns_group:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=0.3)
            except subprocess.TimeoutExpired as exc:
                raise ResetError("The normal-device query could not be stopped; preparation cancelled.") from exc


def read_normal(info, udid="", expected="", *, timeout=12.0):
    """Select once, query that serial, then recheck it; never substitute a model."""
    deadline = time.monotonic() + timeout
    device = _select_normal(udid, deadline=deadline)
    if expected == "iPhone1,1":
        from .physical_usb import require_single_mobile_device
        require_single_mobile_device(deadline=deadline)
    udid = device["udid"]
    _progress("Reading the connected iPhone's identity.")

    def query(arguments):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        result = _run_info([info, "-u", udid, *arguments], timeout=min(2.0, remaining))
        return result.stdout if result is not None and result.returncode == 0 else None


    # The old recovery backend tried only -s once. Some normal devices answer
    # only the ordinary paired query; others expose basic identity without it.
    for arguments in (("-s", "-x"), ("-x",)):
        output = query(arguments)
        if output:
            try:
                values = plistlib.loads(output)
            except (ValueError, plistlib.InvalidFileException, ExpatError):
                continue
            if isinstance(values, dict):
                try:
                    identity = _normal_identity(values, udid, expected)
                except UnreadableNormalDevice:
                    continue
                _select_normal(udid, deadline=deadline)
                if expected == "iPhone1,1":
                    require_single_mobile_device(deadline=deadline)
                return identity
    # A few older tool/device combinations answer individual keys but not XML.
    values = {}
    for key in ("ProductType", "HardwareModel", "UniqueChipID", "ProductVersion"):
        for simple in (True, False):
            output = query([*( ["-s"] if simple else []), "-k", key])
            if output:
                value = output.decode("utf-8", errors="replace").strip()
                if value and len(value) <= 128 and not any(ord(c) < 32 for c in value):
                    values[key] = value
                    break
    identity = _normal_identity(values, udid, expected)
    _select_normal(udid, deadline=deadline)
    if expected == "iPhone1,1":
        require_single_mobile_device(deadline=deadline)
    return identity


def recovery_identity(model, snapshot, expected_ecid="", wanted=None):
    fields = {}
    for key, value in re.findall(r"^([A-Z]+):[ \t]*(.*?)[ \t\r]*$", snapshot, re.MULTILINE):
        if key in fields:
            raise ResetError("Ambiguous recovery identity; no firmware was uploaded.")
        fields[key] = value
    if not snapshot or fields.get("MODE") not in ("Recovery", "DFU", "WTF", "iBSS", "iBEC"):
        return None
    if model not in PHONES:
        raise ResetError("Select the exact connected iPhone model before continuing.")
    try:
        chip, board = int(fields["CPID"], 16), int(fields["BDID"], 16)
    except (KeyError, ValueError):
        return None
    if (chip, board) != PHONES[model][1:]:
        raise ResetError("The recovery USB chip/board does not match the selected iPhone. No firmware was uploaded.")
    try:
        ecid = _ecid(fields.get("ECID"), 16, allow_zero=model == "iPhone1,1")
    except (TypeError, ValueError):
        return None
    if expected_ecid and expected_ecid != "-" and ecid != _ecid(expected_ecid, allow_zero=model == "iPhone1,1"):
        raise ResetError("A different iPhone appeared during the USB transition. No firmware was uploaded.")
    if wanted and fields["MODE"] != wanted:
        return None
    return (fields["MODE"], model, PHONES[model][0], ecid, "Unknown", "-")


def wait_mode(model, ecid, query, *, wanted="Recovery", timeout=50.0, udid="",
              clock=time.monotonic, sleep=time.sleep, phases=None):
    from .recovery_timing import RecoveryProbe
    probe = RecoveryProbe(query, clock=clock)
    phases = phases or (("Waiting for the iPhone to enter " + wanted + " mode.", timeout),)
    try:
        for message, duration in phases:
            deadline = clock() + duration
            last = None
            while clock() < deadline:
                remaining = max(0, math.ceil(deadline - clock()))
                if remaining != last:
                    _progress(message, remaining)
                    last = remaining
                result = probe.poll()
                if result is not None:
                    identity = recovery_identity(model, result, ecid, wanted)
                    if identity is not None:
                        if model == "iPhone1,1":
                            from .physical_usb import require_single_mobile_device
                            require_single_mobile_device(deadline=deadline)
                        # An unrelated recovery phone must not satisfy a wait
                        # while the selected normal-mode phone is still present.
                        normal = _select_normal(udid, required=False, deadline=deadline) if udid else None
                        if normal is None and clock() < deadline:
                            return identity
                sleep(min(0.025, max(0, deadline - clock())))
        raise ResetError("The selected iPhone did not enter verified " + wanted +
                         " mode in time. No firmware was uploaded. Reconnect it and retry the guided steps.")
    finally:
        probe.close()


def manual_recovery(model, ecid, udid, query, reason):
    if model not in PHONES:
        raise ResetError("The USB phone is visible but its identity is unavailable. "
                         "Select its exact model, then enter Recovery mode manually and retry.")
    _continue(reason + " No automatic reboot was sent.\n"
              "To enter Recovery mode manually: disconnect the USB cable, turn the iPhone off, "
              "then hold HOME while reconnecting the cable. Keep holding HOME until the recovery screen appears. "
              "Press Enter to continue; the next step will verify the phone before any firmware upload.")
    return wait_mode(model, ecid, query, udid=udid)


def detect(info, model, udid, query):
    try:
        return read_normal(info, udid, model)
    except NormalServiceUnavailable:
        # DFU/Recovery endpoints do not use usbmuxd. Preserve the backend's
        # independent recovery discovery without accepting any normal identity.
        raise NoNormalDevice() from None
    except UnreadableNormalDevice:
        device = _select_normal(udid)
        return manual_recovery(model, "", device["udid"], query,
                               "The USB phone is connected, but its normal-mode identity could not be read. "
                               "Unlock it and trust this computer if possible, or use the button steps below.")


def enter_recovery(info, enter_tool, model, udid, ecid, query):
    # This is a second live check immediately before the only host reboot call.
    try:
        identity = read_normal(info, udid, model)
    except UnreadableNormalDevice:
        return manual_recovery(model, ecid, udid, query, "The phone no longer permits its normal identity to be read.")
    if identity[3] != _ecid(ecid, allow_zero=model == "iPhone1,1"):
        raise ResetError("The normal-mode phone changed before recovery entry. No device was rebooted.")
    _progress("Sending the selected iPhone to Recovery mode.")
    try:
        result = subprocess.run([enter_tool, identity[5]], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    except subprocess.TimeoutExpired:
        # A timeout does not prove that the command was not delivered. Do not
        # send it again; observe the expected phone or allow manual entry.
        result = None
    if result is None or result.returncode:
        _continue("Automatic Recovery entry was not confirmed. Use the iPhone's HOME button while reconnecting "
                  "its cable to enter Recovery manually, then press Enter to continue. "
                  "Openup will verify the selected phone before proceeding.")
    return wait_mode(model, ecid, query, udid=udid)


def guide_dfu(model, ecid, query):
    _continue("Get ready to enter DFU mode. Keep the iPhone connected and locate its TOP and HOME buttons. "
              "Press Enter to begin the on-screen countdown.")
    return wait_mode(model, ecid, query, wanted="DFU", phases=(
        ("Hold TOP and HOME together.", 10),
        ("Release TOP; keep holding HOME.", 8),
        ("Keep holding HOME while the iPhone reconnects.", 15),
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("detect", "recovery", "dfu", "verify"))
    parser.add_argument("--info", default="")
    parser.add_argument("--enter", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--udid", default="")
    parser.add_argument("--ecid", default="")
    parser.add_argument("--mode", default="")
    parser.add_argument("--query", nargs=argparse.REMAINDER, required=True)
    args = parser.parse_args()
    query = args.query
    try:
        if args.action == "detect":
            identity = detect(args.info, args.model, args.udid, query)
        elif args.action == "recovery":
            identity = enter_recovery(args.info, args.enter, args.model, args.udid, args.ecid, query)
        elif args.action == "dfu":
            identity = guide_dfu(args.model, args.ecid, query)
        else:
            identity = wait_mode(args.model, args.ecid, query, wanted=args.mode, timeout=5)
        print("\t".join(identity), flush=True)
        return 0
    except NoNormalDevice:
        return 1
    except (ResetError, OSError, ValueError) as exc:
        print("[Error] " + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

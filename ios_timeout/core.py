"""Plist planning, durable backups, transactions, and recovery.

The device adapter owns all remote I/O. There is no code guessing, activation,
erasing, setup.app manipulation, or firmware flashing in this module.
"""

import hashlib
import json
import os
import plistlib
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__


class ResetError(Exception):
    pass


PREFS = "/mnt2/mobile/Library/Preferences/com.apple.springboard.plist"
JOURNAL = "/mnt2/mobile/Library/SpringBoard/LockoutStateJournal.plist"
SYSTEM_VERSION = "/mnt1/System/Library/CoreServices/SystemVersion.plist"
ALLOWED_PATHS = (PREFS, JOURNAL)
MAX_FILE = 8 * 1024 * 1024
SUPPORTED = {
    "iPhone3,1": ("iPhone 4", (6, 0, 0), (7, 1, 2)),
    "iPhone3,2": ("iPhone 4", (6, 0, 0), (7, 1, 2)),
    "iPhone3,3": ("iPhone 4", (6, 0, 0), (7, 1, 2)),
    "iPhone4,1": ("iPhone 4S", (6, 0, 0), (9, 3, 6)),
    "iPhone5,1": ("iPhone 5", (6, 0, 0), (10, 3, 4)),
    "iPhone5,2": ("iPhone 5", (6, 0, 0), (10, 3, 4)),
    "iPhone5,3": ("iPhone 5c", (7, 0, 0), (10, 3, 3)),
    "iPhone5,4": ("iPhone 5c", (7, 0, 0), (10, 3, 3)),
}
REMOVE_KEYS = (
    "SBDeviceLockBlockTimeIntervalSinceReferenceDate",
    "SBDeviceLockBlockStateGeneration",
    "SBDeviceLockCachedVCCaps",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def plist(data, label):
    if len(data) > MAX_FILE:
        raise ResetError(f"{label}: file exceeds the {MAX_FILE}-byte limit.")
    try:
        value = plistlib.loads(data)
    except Exception as exc:
        raise ResetError(f"{label}: invalid or unreadable plist ({exc}).") from exc
    if not isinstance(value, dict):
        raise ResetError(f"{label}: expected a dictionary plist.")
    return value


def compatible(model, version):
    if model not in SUPPORTED:
        raise ResetError(
            f"{model!r} is unsupported. This release accepts iPhone 4/4S/5/5c "
            "only; iPhone 5s and newer are excluded."
        )
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        raise ResetError(f"Unrecognized installed iOS version: {version!r}.")
    parts = tuple(int(x) for x in version.split("."))
    parts += (0,) * (3 - len(parts))
    name, low, high = SUPPORTED[model]
    if not low <= parts <= high:
        raise ResetError(f"{name} on iOS {version} is outside this release's range.")
    return name


@dataclass(frozen=True)
class Identity:
    udid: str
    model: str
    version: str
    build: str

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", self.udid):
            raise ResetError("Missing or invalid USB device identifier.")
        if not re.fullmatch(r"[A-Za-z0-9]{3,24}", self.build):
            raise ResetError("Missing or invalid installed iOS build.")
        compatible(self.model, self.version)


@dataclass(frozen=True)
class Metadata:
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class Change:
    path: str
    before: bytes
    after: Optional[bytes]
    metadata: Metadata


@dataclass
class Plan:
    identity: Identity
    changes: list
    notes: list


def make_plan(device, identity):
    identity.validate()
    original, metadata = device.read(PREFS)
    values = plist(original, "SpringBoard preferences")
    known = set(REMOVE_KEYS) | {"SBDeviceLockBlocked", "SBDeviceLockFailedAttempts"}
    if not known.intersection(values):
        raise ResetError("No recognized lockout-state keys found; nothing will be guessed.")
    if values.get("SBDeviceWipe") not in (None, False, 0):
        raise ResetError("A wipe request is present in the preferences; refusing to edit.")
    patched = dict(values)
    patched["SBDeviceLockBlocked"] = False
    patched["SBDeviceLockFailedAttempts"] = 0
    for key in REMOVE_KEYS:
        patched.pop(key, None)
    notes = [
        "Reset failed-attempt count to 0 and blocked state to false.",
        "Remove known cached timer/state entries; preserve other preferences.",
        "The correct passcode is still required. Future incorrect attempts can lock it again.",
    ]
    if values.get("SBDeviceWipeEnabled"):
        notes.append("Erase-after-failed-attempts is enabled and will remain enabled.")
    changes = []
    # Compare the serialized format as well as values: bool and int compare equal
    # in Python, but plist boolean and integer have different meanings.
    fmt = plistlib.FMT_BINARY if original.startswith(b"bplist00") else plistlib.FMT_XML
    after = plistlib.dumps(patched, fmt=fmt, sort_keys=False)
    before_normal = plistlib.dumps(values, fmt=fmt, sort_keys=False)
    if after != before_normal:
        changes.append(Change(PREFS, original, after, metadata))
    try:
        journal, journal_meta = device.read(JOURNAL)
    except FileNotFoundError:
        notes.append("No LockoutStateJournal.plist exists; none will be created.")
    else:
        plist(journal, "Lockout journal")
        changes.append(Change(JOURNAL, journal, None, journal_meta))
        notes.append("Archive the old lockout journal so iOS can recreate it.")
    return Plan(identity, changes, notes)


def secure_write(path, data):
    """Atomic, private, fsynced host-side write."""
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_manifest(directory, manifest):
    secure_write(Path(directory) / "manifest.json",
                 (json.dumps(manifest, indent=2) + "\n").encode())


def remote_names(path, token):
    parent, filename = path.rsplit("/", 1)
    prefix = f"{parent}/.itr-{token}-{filename}"
    return prefix + ".original", prefix + ".staged"


def make_backup(plan, root):
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = root / f"{stamp}-{token}"
    folder.mkdir(mode=0o700)
    manifest = {
        "schema": 1, "tool_version": __version__, "transaction": token,
        "created_utc": stamp, "identity": asdict(plan.identity),
        "status": "backed_up", "files": [],
    }
    for index, change in enumerate(plan.changes):
        filename = f"{index:02d}-{change.path.rsplit('/', 1)[1]}"
        secure_write(folder / filename, change.before)
        original_path, stage_path = remote_names(change.path, token)
        manifest["files"].append({
            "path": change.path, "backup": filename,
            "before_sha256": digest(change.before),
            "after_sha256": digest(change.after) if change.after is not None else None,
            "metadata": asdict(change.metadata),
            "original_path": original_path, "stage_path": stage_path,
        })
    save_manifest(folder, manifest)
    return folder, manifest


def same(device, path, expected, metadata=None):
    data, actual = device.read(path)
    if digest(data) != digest(expected) or (metadata is not None and actual != metadata):
        raise ResetError(f"{path}: contents or ownership changed; refusing to continue.")


def restore_files(device, manifest, originals):
    """Recover from any rename boundary without overwriting unexpected content."""
    # Validate the entire recovery before changing either live file.
    for item, data in zip(manifest["files"], originals):
        if device.exists(item["original_path"]):
            same(device, item["original_path"], data, Metadata(**item["metadata"]))
            if device.exists(item["path"]):
                current, _ = device.read(item["path"])
                if digest(current) not in (item["before_sha256"], item["after_sha256"]):
                    raise ResetError(f"{item['path']}: changed since reset; recovery stopped.")
        else:
            same(device, item["path"], data, Metadata(**item["metadata"]))
    for item, data in zip(reversed(manifest["files"]), reversed(originals)):
        path = item["path"]
        original_path = item["original_path"]
        if device.exists(original_path):
            same(device, original_path, data, Metadata(**item["metadata"]))
            if device.exists(path):
                current, _ = device.read(path)
                if digest(current) not in (item["before_sha256"], item["after_sha256"]):
                    raise ResetError(f"{path}: changed since reset; recovery stopped.")
            device.rename(original_path, path, replace=True)
        else:
            # Before the first rename, or already restored after an interruption.
            same(device, path, data, Metadata(**item["metadata"]))
        if device.exists(item["stage_path"]):
            # Interrupted uploads can leave any prefix here. Once the original
            # is restored, this transaction's uniquely named stage is disposable.
            device.read(item["stage_path"])  # Still reject symlinks/non-regular files.
            device.remove(item["stage_path"])
        same(device, path, data, Metadata(**item["metadata"]))
    device.sync()


def apply_plan(device, plan, backup_root):
    if not plan.changes:
        return None
    folder, manifest = make_backup(plan, backup_root)
    token = manifest["transaction"]
    locked = False
    mutated = False
    try:
        device.acquire(token)
        locked = True
        # Recheck every original before making any device changes.
        for change, item in zip(plan.changes, manifest["files"]):
            same(device, change.path, change.before, change.metadata)
            if device.exists(item["original_path"]) or device.exists(item["stage_path"]):
                raise ResetError("Transaction filenames already exist.")
        manifest["status"] = "staging"
        save_manifest(folder, manifest)
        for change, item in zip(plan.changes, manifest["files"]):
            if change.after is not None:
                mutated = True
                device.stage(change.path, item["stage_path"], change.after, change.metadata)
                same(device, item["stage_path"], change.after, change.metadata)
        manifest["status"] = "committing"
        save_manifest(folder, manifest)
        for change, item in zip(plan.changes, manifest["files"]):
            same(device, change.path, change.before, change.metadata)
            mutated = True
            device.rename(change.path, item["original_path"])
            if change.after is not None:
                device.rename(item["stage_path"], change.path)
        device.sync()
        for change in plan.changes:
            if change.after is None:
                if device.exists(change.path):
                    raise ResetError("Lockout journal unexpectedly reappeared.")
            else:
                same(device, change.path, change.after, change.metadata)
        manifest["status"] = "committed"
        save_manifest(folder, manifest)
        device.release(token)
        locked = False
        return folder
    except BaseException as exc:
        # Includes Ctrl-C: attempt rollback before leaving a partial transaction.
        recovery = None
        if locked:
            try:
                if mutated:
                    restore_files(device, manifest, [c.before for c in plan.changes])
                device.release(token)
                locked = False
            except BaseException as rollback_exc:
                recovery = str(rollback_exc)
        manifest["status"] = "recovery_required" if recovery else "rolled_back"
        manifest["error"] = str(exc)
        if recovery:
            manifest["recovery_error"] = recovery
        try:
            save_manifest(folder, manifest)
        except Exception:
            pass  # The original durable manifest and original files still exist.
        detail = ("Recovery is required; keep the phone in its ramdisk."
                  if recovery else "No reset was committed; original files are intact.")
        raise ResetError(f"{exc}\n{detail}\nBackup/recovery directory: {folder}") from exc


def load_backup(folder):
    folder = Path(folder).expanduser().resolve()
    manifest_path = folder / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 128 * 1024:
        raise ResetError("Invalid backup manifest.")
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest["schema"] != 1:
            raise ValueError("unsupported schema")
        token = manifest["transaction"]
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("invalid transaction identifier")
        identity = Identity(**manifest["identity"])
        identity.validate()
        entries = manifest["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 2:
            raise ValueError("invalid file list")
        originals = []
        seen = set()
        for index, item in enumerate(entries):
            path = item["path"]
            if path not in ALLOWED_PATHS or path in seen:
                raise ValueError("unapproved or duplicate remote path")
            seen.add(path)
            if (item["original_path"], item["stage_path"]) != remote_names(path, token):
                raise ValueError("invalid transaction paths")
            expected_name = f"{index:02d}-{path.rsplit('/', 1)[1]}"
            if item["backup"] != expected_name:
                raise ValueError("invalid backup filename")
            backup = folder / expected_name
            if backup.is_symlink() or backup.stat().st_size > MAX_FILE:
                raise ValueError("invalid backup file")
            data = backup.read_bytes()
            if digest(data) != item["before_sha256"]:
                raise ValueError("backup checksum mismatch")
            plist(data, "Backup")
            meta = Metadata(**item["metadata"])
            if any(type(v) is not int or v < 0 for v in asdict(meta).values()):
                raise ValueError("invalid file metadata")
            if not stat.S_ISREG(meta.mode):
                raise ValueError("backup is not a regular file")
            after = item["after_sha256"]
            if after is not None and not re.fullmatch(r"[a-f0-9]{64}", after):
                raise ValueError("invalid post-reset checksum")
            originals.append(data)
        return folder, manifest, identity, originals
    except (KeyError, TypeError, ValueError) as exc:
        raise ResetError(f"Invalid backup manifest: {exc}") from exc


def restore_backup(device, identity, folder):
    folder, manifest, saved_identity, originals = load_backup(folder)
    if identity != saved_identity:
        raise ResetError("Backup device/model/iOS/build do not match the connected phone.")
    token = manifest["transaction"]
    device.acquire(token, recover=True)
    try:
        restore_files(device, manifest, originals)
        manifest["status"] = "restored"
        save_manifest(folder, manifest)
        device.release(token)
    except BaseException as exc:
        manifest["status"] = "recovery_required"
        try:
            save_manifest(folder, manifest)
        except Exception:
            pass
        raise ResetError(
            f"Restore stopped: {exc}\nKeep the phone in its ramdisk. Backup: {folder}"
        ) from exc
    return folder

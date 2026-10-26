"""Delete one explicitly selected, flat local backup without recursive removal."""

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .core import MAX_FILE, ResetError, load_backup
from .host import host_lock, open_regular, reject_link


TRANSACTION_FOLDER = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}")
BACKUP_FILES = {"manifest.json", "00-com.apple.springboard.plist",
                "00-LockoutStateJournal.plist", "01-LockoutStateJournal.plist"}


@dataclass(frozen=True)
class BackupFile:
    name: str
    identity: tuple
    checksum: str


@dataclass(frozen=True)
class BackupDeletion:
    path: Path
    directory: tuple
    files: tuple
    valid: bool
    status: str


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _plain_directory(path):
    path = Path(path).expanduser().absolute()
    for component in (path, *path.parents):
        reject_link(component)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ResetError("Choose a backup transaction folder, not a file.")
    return path.resolve()


def _file_snapshot(path, name, directory_fd=None):
    if directory_fd is None:
        reject_link(path / name)
        fd = open_regular(path / name, os.O_RDONLY)
    else:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        limit = 128 * 1024 if name == "manifest.json" else MAX_FILE
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ResetError("Backup deletion accepts only bounded regular backup files.")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(data) != before.st_size or _identity(before) != _identity(after):
            raise ResetError("The backup changed while being checked. Refresh and try again.")
        return BackupFile(name, _identity(before), hashlib.sha256(data).hexdigest())


def _snapshot(path, directory_fd=None):
    directory = os.fstat(directory_fd) if directory_fd is not None else path.lstat()
    names = sorted(os.listdir(directory_fd if directory_fd is not None else path))
    files = [_file_snapshot(path, name, directory_fd) for name in names]
    return _identity(directory), tuple(files)


def prepare_backup_deletion(folder, state):
    """Read-only scope/content snapshot to show before either confirmation."""
    try:
        path = _plain_directory(folder)
        state = Path(state).expanduser().absolute().resolve()
        root = state / "backups"
        if path in {root, state, *state.parents}:
            raise ResetError("Select one backup transaction, not the backups or application-state folder.")
        managed = path.parent == root and TRANSACTION_FOLDER.fullmatch(path.name) is not None
        try:
            _, manifest, _, _ = load_backup(path)
            expected = {"manifest.json", *(entry["backup"] for entry in manifest["files"])}
            valid, status = True, str(manifest.get("status", "unknown"))
        except (ResetError, OSError, ValueError):
            if not managed:
                raise ResetError("A manually chosen folder must contain a valid backup before Openup can delete it.")
            expected = BACKUP_FILES
            valid, status = False, "Invalid backup"
        names = set(os.listdir(path))
        if (valid and names != expected) or (not valid and not names <= expected):
            raise ResetError("This folder contains unexpected files or folders. Openup will not delete unrelated contents.")
        directory, files = _snapshot(path)
        captured_names = {item.name for item in files}
        if (valid and captured_names != expected) or (not valid and not captured_names <= expected):
            raise ResetError("The backup contents changed while being checked. Refresh and try again.")
        return BackupDeletion(path, directory, files, valid, status)
    except OSError as exc:
        raise ResetError(f"Cannot safely delete this backup: {exc}") from exc


def delete_backup(snapshot, state):
    """Called only after both confirmations; revalidate under the app's host lock."""
    removed = 0
    try:
        with host_lock(state):
            if prepare_backup_deletion(snapshot.path, state) != snapshot:
                raise ResetError("The backup changed after confirmation. Nothing was deleted; refresh and try again.")
            if os.name != "nt":
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                parent_fd = os.open(snapshot.path.parent, flags)
                try:
                    folder_fd = os.open(snapshot.path.name, flags, dir_fd=parent_fd)
                    try:
                        if _snapshot(snapshot.path, folder_fd) != (snapshot.directory, snapshot.files):
                            raise ResetError("The backup changed after confirmation. Nothing was deleted.")
                        for item in snapshot.files:
                            if _identity(os.stat(item.name, dir_fd=folder_fd, follow_symlinks=False)) != item.identity:
                                raise ResetError("The backup changed during deletion.")
                            os.unlink(item.name, dir_fd=folder_fd)
                            removed += 1
                        current = os.stat(snapshot.path.name, dir_fd=parent_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != snapshot.directory[:2]:
                            raise ResetError("The backup folder moved during deletion.")
                        os.rmdir(snapshot.path.name, dir_fd=parent_fd)
                    finally:
                        os.close(folder_fd)
                finally:
                    os.close(parent_fd)
            else:
                # Windows lacks dir_fd unlink. Never recurse: reject reparse
                # points and recheck the directory and each exact file before
                # unlinking. A final symlink is never followed by unlink.
                for item in snapshot.files:
                    _plain_directory(snapshot.path)
                    directory = snapshot.path.lstat()
                    if ((directory.st_dev, directory.st_ino) != snapshot.directory[:2]
                            # Windows path stat and descriptor stat can report
                            # different identities for the same file. Reopen
                            # safely and compare descriptor metadata + bytes.
                            or _file_snapshot(snapshot.path, item.name) != item):
                        raise ResetError("The backup changed during deletion.")
                    (snapshot.path / item.name).unlink()
                    removed += 1
                _plain_directory(snapshot.path)
                directory = snapshot.path.lstat()
                if (directory.st_dev, directory.st_ino) != snapshot.directory[:2]:
                    raise ResetError("The backup folder moved during deletion.")
                snapshot.path.rmdir()
    except (OSError, ResetError) as exc:
        if removed:
            raise ResetError(f"Backup deletion did not finish. Some files were removed. Remaining folder: {snapshot.path}\n{exc}") from exc
        raise ResetError(str(exc)) from exc

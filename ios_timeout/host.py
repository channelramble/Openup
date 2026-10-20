"""Host OS differences; device paths and metadata remain POSIX."""

import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path


def state_directory():
    from . import legacy_names
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        current, previous = base / "Openup", base / legacy_names.APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
        current, previous = base / "Openup", base / legacy_names.APP_NAME
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        current, previous = base / "openup", base / legacy_names.STATE_SLUG
    # Keep the same backups, transaction paths and host lock for existing users.
    # Renaming or copying a live workspace could split its lock or break recovery.
    return previous if previous.is_dir() and not current.exists() else current


def reject_link(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError(f"Symlinks and Windows reparse points are not allowed: {path}")


def open_regular(path, flags, mode=0o600):
    """Open without following the final link, including Windows reparse points."""
    if sys.platform == "win32":
        reject_link(path)
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        access = 0x80000000 | (0x40000000 if flags & (os.O_RDWR | os.O_WRONLY) else 0)
        handle = create(str(Path(path).absolute()), access, 7, None,
                        4 if flags & os.O_CREAT else 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        # Transfer ownership of the handle to the CRT immediately.
        crt_flags = (os.O_RDWR if flags & (os.O_RDWR | os.O_WRONLY) else os.O_RDONLY) | os.O_BINARY
        try:
            fd = msvcrt.open_osfhandle(handle, crt_flags)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close(handle)
            raise
    else:
        fd = os.open(str(path), flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        attributes = os.fstat(fd)
        if (not stat.S_ISREG(attributes.st_mode)
                or getattr(attributes, "st_file_attributes", 0) & 0x400):
            raise OSError(f"Expected a regular file: {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def sync_directory(path):
    # Windows commits file contents with fsync and replacement with WRITE_THROUGH.
    # POSIX additionally supports fsync on a directory descriptor.
    if sys.platform != "win32":
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def replace_file(source, destination):
    if sys.platform != "win32":
        os.replace(str(source), str(destination))
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    if not move(str(source), str(destination), 0x1 | 0x8):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def host_lock(state_dir):
    from .core import ResetError
    state_dir = Path(state_dir)
    try:
        reject_link(state_dir)
    except OSError as exc:
        raise ResetError(str(exc)) from exc
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != "win32":
        if state_dir.stat().st_uid != os.getuid():
            raise ResetError("The state directory must belong to the current user.")
        state_dir.chmod(0o700)
    fd = open_regular(state_dir / "session.lock", os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        if sys.platform == "win32":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ResetError("Another instance is already using this state directory.") from exc
        else:
            import fcntl
            if os.fstat(fd).st_uid != os.getuid():
                raise ResetError("Invalid local session lock owner.")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ResetError("Another instance is already using this state directory.") from exc
        locked = True
        yield
    finally:
        if locked and sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)

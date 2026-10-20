"""SSH/SFTP through a socket connected to a single, selected USB device."""

import base64
import errno
import hashlib
import json
import re
import shlex
import stat
import time
from pathlib import Path, PurePosixPath

from . import usbmux
from .core import (
    Identity, MAX_FILE, Metadata, ResetError, SUPPORTED, SYSTEM_VERSION,
    plist, secure_write,
)

BOARD_MODELS = {
    "n90ap": "iPhone3,1", "n90bap": "iPhone3,2", "n92ap": "iPhone3,3",
    "n94ap": "iPhone4,1", "n41ap": "iPhone5,1", "n42ap": "iPhone5,2",
    "n48ap": "iPhone5,3", "n49ap": "iPhone5,4",
}
LOCK = "/mnt2/.iphone-timeout-reset.lock"


def metadata(attributes):
    return Metadata(attributes.st_mode, attributes.st_uid, attributes.st_gid)


class USBDevice:
    def __init__(self, selected, state_dir, password, port=22, trust_new_key=False):
        self.selected = selected
        self.transport = None
        self.sftp = None
        self.socket = None
        if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", selected["udid"]):
            raise ResetError("Unrecognized USB serial format.")
        try:
            import paramiko
        except ImportError as exc:
            raise ResetError("Install dependencies first: python3 -m pip install .") from exc
        try:
            self.socket = usbmux.tunnel(selected["device_id"], port)
            self.transport = paramiko.Transport(self.socket)
            self.transport.start_client(timeout=15)
            key = self.transport.get_remote_server_key()
            self.fingerprint = "SHA256:" + base64.b64encode(
                hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
            state_dir = Path(state_dir).expanduser().resolve()
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            key_path = state_dir / f"ssh-{selected['udid']}.json"
            key_record = {"type": key.get_name(), "key": key.get_base64()}
            if key_path.exists():
                if key_path.is_symlink() or key_path.stat().st_size > 16384:
                    raise ResetError("Invalid saved SSH host-key record.")
                previous = json.loads(key_path.read_text())
                if previous != key_record and not trust_new_key:
                    raise ResetError(
                        "The ramdisk SSH host key changed. Check that the selected phone "
                        "and ramdisk are correct, then use --trust-new-key once."
                    )
            # One known ramdisk SSH credential, never the iPhone passcode.
            self.transport.auth_password("root", password, fallback=False)
            secure_write(key_path, json.dumps(key_record).encode())
            self.sftp = paramiko.SFTPClient.from_transport(self.transport)
            self.sftp.get_channel().settimeout(20)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.sftp is not None:
            self.sftp.close()
        if self.transport is not None:
            self.transport.close()
        if self.socket is not None:
            self.socket.close()

    def command(self, command, timeout=30):
        channel = self.transport.open_session(timeout=15)
        channel.settimeout(15)
        stdout, stderr = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            channel.exec_command(command)
            while True:
                if channel.recv_ready():
                    stdout.extend(channel.recv(32768))
                if channel.recv_stderr_ready():
                    stderr.extend(channel.recv_stderr(32768))
                if len(stdout) + len(stderr) > MAX_FILE:
                    raise ResetError("Ramdisk command returned too much data.")
                if (channel.exit_status_ready() and not channel.recv_ready()
                        and not channel.recv_stderr_ready()):
                    break
                if time.monotonic() > deadline:
                    raise ResetError(f"Ramdisk command timed out: {command}")
                time.sleep(0.02)
            code = channel.recv_exit_status()
            if code != 0:
                detail = stderr.decode("utf-8", "replace").strip()[-1000:]
                raise ResetError(f"Ramdisk command failed ({code}): {command}\n{detail}")
            return stdout.decode("utf-8", "replace")
        finally:
            channel.close()

    def safe_parents(self, path):
        parsed = PurePosixPath(path)
        if (not parsed.is_absolute() or ".." in parsed.parts
                or not (path.startswith("/mnt1/") or path.startswith("/mnt2/"))):
            raise ResetError(f"Unapproved filesystem path: {path}")
        for parent in reversed(parsed.parents):
            if str(parent) == "/":
                continue
            attributes = self.sftp.lstat(str(parent))
            if not stat.S_ISDIR(attributes.st_mode):
                raise ResetError(f"Parent is a symlink or non-directory: {parent}")

    def exists(self, path):
        self.safe_parents(path)
        try:
            self.sftp.lstat(path)
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return False
            raise

    def read(self, path):
        self.safe_parents(path)
        try:
            before = self.sftp.lstat(path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError(path) from exc
            raise
        if not stat.S_ISREG(before.st_mode):
            raise ResetError(f"{path}: symlinks and non-regular files are unsupported.")
        if before.st_size > MAX_FILE:
            raise ResetError(f"{path}: file is too large.")
        with self.sftp.open(path, "rb") as stream:
            data = stream.read(MAX_FILE + 1)
        after = self.sftp.lstat(path)
        if (len(data) != before.st_size or metadata(before) != metadata(after)
                or before.st_mtime != after.st_mtime or before.st_size != after.st_size):
            raise ResetError(f"{path}: file changed while being read.")
        return data, metadata(before)

    def stage(self, source, destination, contents, original_metadata):
        self.safe_parents(source)
        self.safe_parents(destination)
        if self.exists(destination):
            raise ResetError(f"Staging destination exists: {destination}")
        # Darwin cp -p retains original metadata; truncating the copy keeps its
        # inode. The original inode is archived unchanged during commit.
        self.command("/bin/cp -p " + shlex.quote(source) + " " + shlex.quote(destination))
        with self.sftp.open(destination, "r+b") as stream:
            stream.truncate(0)
            stream.write(contents)
            stream.flush()
        self.sftp.chown(destination, original_metadata.uid, original_metadata.gid)
        self.sftp.chmod(destination, stat.S_IMODE(original_metadata.mode))

    def rename(self, source, destination, replace=False):
        self.safe_parents(source)
        self.safe_parents(destination)
        if not replace:
            if self.exists(destination):
                raise ResetError(f"Rename destination already exists: {destination}")
            self.sftp.rename(source, destination)
        else:
            try:
                self.sftp.posix_rename(source, destination)
            except OSError as exc:
                if (exc.errno not in (errno.ENOSYS, errno.EOPNOTSUPP)
                        and "unsupported" not in str(exc).lower()):
                    raise
                self.command("/bin/mv -f " + shlex.quote(source) + " " + shlex.quote(destination))

    def remove(self, path):
        self.safe_parents(path)
        self.sftp.remove(path)

    def sync(self):
        self.command("/bin/sync")

    def acquire(self, token, recover=False):
        self.safe_parents(LOCK)
        try:
            self.sftp.mkdir(LOCK, mode=0o700)
        except OSError as exc:
            if not self.exists(LOCK):
                raise
            attributes = self.sftp.lstat(LOCK)
            if not stat.S_ISDIR(attributes.st_mode):
                raise ResetError("Device transaction lock is not a directory.") from exc
            try:
                owner, _ = self.read(LOCK + "/owner")
            except (OSError, ResetError) as read_exc:
                raise ResetError("An incomplete transaction lock exists on the phone.") from read_exc
            if not recover or owner != token.encode():
                raise ResetError(
                    "A device transaction lock already exists. Restore its backup first."
                ) from exc
            return
        with self.sftp.open(LOCK + "/owner", "wx") as stream:
            stream.write(token.encode())
        self.sftp.chmod(LOCK + "/owner", 0o600)

    def release(self, token):
        owner, _ = self.read(LOCK + "/owner")
        if owner != token.encode():
            raise ResetError("Device transaction lock owner changed.")
        self.sftp.remove(LOCK + "/owner")
        self.sftp.rmdir(LOCK)

    def inspect(self, mount=False):
        model = None
        for command in ("/usr/sbin/sysctl -n hw.machine", "/usr/sbin/sysctl -n hw.model",
                        "/sbin/sysctl -n hw.machine", "/sbin/sysctl -n hw.model"):
            try:
                answer = self.command(command).strip()
            except ResetError:
                continue
            if answer.startswith("iPhone"):
                model = answer
                break
            if answer.lower() in BOARD_MODELS:
                model = BOARD_MODELS[answer.lower()]
                break
        if model not in SUPPORTED:
            raise ResetError(f"Unsupported or unidentifiable hardware: {model!r}.")
        mounts = self.command("/sbin/mount")
        if not re.search(r"^/dev/md\d+ on / \(", mounts, re.MULTILINE):
            raise ResetError("The running root filesystem is not a recognized SSH ramdisk.")
        processes = self.command(
            "if test -x /bin/ps; then /bin/ps -A -o comm=; "
            "elif test -x /bin/launchctl; then /bin/launchctl list; fi"
        )
        if re.search(r"(?:^|/|\.|\s)SpringBoard(?:\s|$)", processes):
            raise ResetError("SpringBoard is running. Use an offline SSH ramdisk.")
        def mounted_volumes(output):
            result = set()
            for number in ("1", "2"):
                found = re.search(r"^(\S+) on /mnt" + number + r" \(", output, re.MULTILINE)
                if found:
                    if found.group(1) != "/dev/disk0s1s" + number:
                        raise ResetError(f"Unexpected volume mounted at /mnt{number}.")
                    result.add(number)
            return result

        volumes = mounted_volumes(mounts)
        mounted = len(volumes) == 2
        if not mounted and mount:
            # Use only the known HFS partitions, without mount.sh's repair
            # fallback for other disk layouts.
            self.command("test -b /dev/disk0s1s1 && test -b /dev/disk0s1s2")
            for number in ("1", "2"):
                if number not in volumes:
                    attributes = self.sftp.lstat("/mnt" + number)
                    if not stat.S_ISDIR(attributes.st_mode):
                        raise ResetError(f"/mnt{number} is not a regular mount directory.")
                    self.command(
                        f"/sbin/mount_hfs /dev/disk0s1s{number} /mnt{number}", timeout=60
                    )
            mounts = self.command("/sbin/mount")
            mounted = len(mounted_volumes(mounts)) == 2
        if not mounted:
            raise ResetError(
                "Expected legacy system/data volumes are not mounted. "
                "Retry with --mount to mount the known legacy HFS volumes. "
                "Filesystem repair is not performed automatically."
            )
        version_data, _ = self.read(SYSTEM_VERSION)
        installed = plist(version_data, "Installed SystemVersion.plist")
        identity = Identity(self.selected["udid"], model,
                            installed.get("ProductVersion", ""),
                            installed.get("ProductBuildVersion", ""))
        identity.validate()
        return identity

    def reboot(self):
        self.sync()
        self.command("test -x /sbin/reboot_bak || test -x /sbin/reboot")
        # Ramdisk builds sometimes rename reboot. Disconnect before receipt of
        # an exit status is expected, so report only that a request was sent.
        channel = self.transport.open_session(timeout=15)
        try:
            channel.exec_command(
                "if test -x /sbin/reboot_bak; then /sbin/reboot_bak; else /sbin/reboot; fi"
            )
        finally:
            channel.close()

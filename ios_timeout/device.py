"""SSH/SFTP through a socket connected to a single, selected USB device."""

import base64
import errno
import hashlib
import json
import re
import shlex
import stat
import time
import threading
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath

from . import usbmux
from .core import (
    Identity, LockBusy, MAX_FILE, Metadata, ResetError, SUPPORTED, SYSTEM_VERSION,
    plist, read_local_file, secure_write,
)

BOARD_MODELS = {
    "n82ap": "iPhone1,2",
    "n90ap": "iPhone3,1", "n90bap": "iPhone3,2", "n92ap": "iPhone3,3",
    "n94ap": "iPhone4,1", "n41ap": "iPhone5,1", "n42ap": "iPhone5,2",
    "n48ap": "iPhone5,3", "n49ap": "iPhone5,4",
}
LOCK = "/mnt2/.iphone-timeout-reset.lock"
IO_TIMEOUT = 20


def metadata(attributes):
    if any(type(v) is not int or v < 0
           for v in (attributes.st_mode, attributes.st_uid, attributes.st_gid)):
        raise ResetError("SFTP returned incomplete file ownership/mode metadata.")
    return Metadata(attributes.st_mode, attributes.st_uid, attributes.st_gid)


@contextmanager
def channel_deadline(channel, seconds):
    """Bound SSH request acknowledgments as well as subsequent I/O."""
    expired = threading.Event()

    def abort():
        expired.set()
        with suppress(Exception):
            channel.close()

    timer = threading.Timer(seconds, abort)
    timer.daemon = True
    timer.start()
    try:
        yield
        if expired.is_set():
            raise ResetError("SSH operation exceeded its deadline.")
    except Exception as exc:
        if expired.is_set():
            raise ResetError("SSH operation exceeded its deadline.") from exc
        raise
    finally:
        timer.cancel()


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
            self.socket = self._open_tunnel(selected, port)
            from .ssh_compat import usb_transport
            self.transport = usb_transport(self.socket)
            self.transport.banner_timeout = 15
            self.transport.auth_timeout = 15
            self.transport.handshake_timeout = 15
            self.transport.channel_timeout = 15
            self.transport.start_client(timeout=15)
            key = self.transport.get_remote_server_key()
            self.fingerprint = "SHA256:" + base64.b64encode(
                hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
            state_dir = Path(state_dir).expanduser().resolve()
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            key_path = state_dir / f"ssh-{selected['udid']}.json"
            key_record = {"type": key.get_name(), "key": key.get_base64()}
            if key_path.exists() or key_path.is_symlink():
                previous = json.loads(read_local_file(key_path, 16384))
                if previous != key_record and not trust_new_key:
                    raise ResetError(
                        "The ramdisk SSH host key changed. Check that the selected phone "
                        "and ramdisk are correct, then use --trust-new-key once."
                    )
            # One known ramdisk SSH credential, never the iPhone passcode.
            self.transport.auth_password("root", password, fallback=False)
            secure_write(key_path, json.dumps(key_record).encode())
            channel = self.transport.open_session(timeout=15)
            try:
                channel.settimeout(IO_TIMEOUT)
                with channel_deadline(channel, IO_TIMEOUT):
                    channel.invoke_subsystem("sftp")
                    self.sftp = paramiko.SFTPClient(channel)
            except BaseException:
                with suppress(Exception):
                    channel.close()
                raise
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        # Cleanup must not hide a transaction/recovery error.
        for attribute in ("sftp", "transport", "socket"):
            connection = getattr(self, attribute, None)
            setattr(self, attribute, None)
            if connection is not None:
                with suppress(Exception):
                    connection.close()

    def _open_tunnel(self, selected, port):
        # A phone reaching the Mac through a USB-C dock or hub can briefly drop
        # off usbmuxd while it re-enumerates, notably right after the ramdisk
        # boot on Apple Silicon Macs. The phone was already observed present
        # before this point, so retry the usbmux connection for a few seconds,
        # re-resolving the serial each time, instead of failing on a single
        # re-enumeration bounce. A genuinely absent phone has already raised in
        # wait_for_device, so this does not stall missing-device operations.
        deadline = time.monotonic() + 8.0
        while True:
            try:
                return usbmux.tunnel(selected["device_id"], port, expected_udid=selected["udid"])
            except ResetError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def command(self, command, timeout=30):
        channel = self.transport.open_session(timeout=15)
        channel.settimeout(15)
        try:
            with channel_deadline(channel, timeout):
                return self._command_output(channel, command, timeout)
        finally:
            with suppress(Exception):
                channel.close()

    def _command_output(self, channel, command, timeout):
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
            with suppress(Exception):
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
        if type(before.st_size) is not int or not 0 <= before.st_size <= MAX_FILE:
            raise ResetError(f"{path}: invalid or excessive file size.")
        with channel_deadline(self.sftp.get_channel(), IO_TIMEOUT):
            with self.sftp.open(path, "rb") as stream:
                data = stream.read(MAX_FILE + 1)
        after = self.sftp.lstat(path)
        if (not stat.S_ISREG(after.st_mode)
                or len(data) != before.st_size or metadata(before) != metadata(after)
                or before.st_mtime != after.st_mtime or before.st_size != after.st_size):
            raise ResetError(f"{path}: file changed while being read.")
        return data, metadata(before)

    def stage(self, source, destination, contents, original_metadata):
        self.safe_parents(source)
        self.safe_parents(destination)
        if self.exists(destination):
            raise ResetError(f"Staging destination exists: {destination}")
        # Create the staged replacement directly over SFTP. The minimal Legacy
        # iOS Kit ramdisk ships no `cp`, and a copied original would be truncated
        # and overwritten here anyway. Commit archives the untouched original by
        # renaming the live file aside, so the stage file only needs the new
        # contents plus the original's metadata, reapplied below.
        with channel_deadline(self.sftp.get_channel(), IO_TIMEOUT):
            with self.sftp.open(destination, "wb") as stream:
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
        # Flush buffered writes where a `sync` tool exists (full iOS, the 4.x
        # recovery ramdisk). The minimal Legacy iOS Kit timer ramdisk ships no
        # `sync` binary anywhere on its PATH, and its OpenSSH 5.2 has no SFTP
        # fsync, so there is no in-session flush to call. Durability is then
        # completed by the clean reboot (reboot(2) flushes mounted volumes), and
        # crash-recovery stays state-based via the manifest, not sync-dependent.
        # Resolve a sync binary first: if one exists, run it and let a genuine
        # failure propagate so the transaction rolls back; if none exists, no-op.
        path = self.command(
            "for p in /bin/sync /sbin/sync /usr/bin/sync /usr/sbin/sync; do "
            'if test -x "$p"; then printf %s "$p"; exit 0; fi; done; '
            "command -v sync 2>/dev/null || true"
        ).strip()
        if path:
            self.command(path)

    def acquire(self, token, recover=False):
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ResetError("Invalid lock transaction identifier.")
        self.safe_parents(LOCK)
        # The link target *is* the owner token. Creating one directory entry is
        # atomic, unlike mkdir followed by a separate owner-file write.
        try:
            self.sftp.symlink(token, LOCK)
        except OSError as exc:
            owner, _ = self._lock_owner()
            if owner is None:
                raise
            if not recover or owner != token:
                raise LockBusy(
                    f"Device transaction {owner} holds the lock. Restore or finalize its backup first."
                ) from exc
        self.sync()

    def _lock_owner(self):
        self.safe_parents(LOCK)
        try:
            attributes = self.sftp.lstat(LOCK)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None, False
            raise
        legacy = stat.S_ISDIR(attributes.st_mode)
        if legacy:
            try:
                owner = self.read(LOCK + "/owner")[0].decode("ascii")
            except (OSError, UnicodeError) as exc:
                raise ResetError("An incomplete 1.0 lock exists; keep the ramdisk running.") from exc
        elif stat.S_ISLNK(attributes.st_mode):
            owner = self.sftp.readlink(LOCK)
        else:
            raise ResetError("Device transaction lock has an unexpected file type.")
        if not re.fullmatch(r"[a-f0-9]{32}", owner):
            raise ResetError("Device transaction lock contains an invalid owner.")
        return owner, legacy

    def release(self, token):
        owner, legacy = self._lock_owner()
        if owner is None:
            return  # Also handles a successful unlink whose acknowledgment was lost.
        if owner != token:
            raise LockBusy("Device transaction lock owner changed.")
        if legacy:
            self.sftp.remove(LOCK + "/owner")
            self.sftp.rmdir(LOCK)
        else:
            self.sftp.remove(LOCK)
        self.sync()

    def _flat_3g_partition_layout(self):
        """True only for the genuine flat iPhone 3G (S5L8900) partition scheme.

        The project's own ramdisk sources (``mount_partitions.sh``,
        ``util.c`` mountDataPartition) distinguish three disk0 schemes:
          * iOS 5+ LwVM:   system ``/dev/disk0s1s1``, data ``/dev/disk0s1s2``
          * iOS 4 nested:  system ``/dev/disk0s1``,   data ``/dev/disk0s2s1``
                           (``/dev/disk0s2`` is the container, still a block node)
          * flat (3G):     system ``/dev/disk0s1``,   data ``/dev/disk0s2``
        Only the flat scheme is the iPhone 3G, and ``inspect`` mounts it as
        ``/dev/disk0s1`` + ``/dev/disk0s2``. Match that scheme exactly: both flat
        slices present AND none of the LwVM/nested slices present. A ``test -b``
        on ``/dev/disk0s2`` alone is not enough, because the nested scheme also
        has a ``/dev/disk0s2`` block container; requiring the absence of
        ``/dev/disk0s2s1`` (and the LwVM slices) is what rejects the nested and
        iOS 5+ layouts, so a non-flat device is never taken for a 3G. Read live
        from the booted ramdisk, so it works when no kernel query tool exists.
        """
        try:
            self.command(
                "test -b /dev/disk0s1 && test -b /dev/disk0s2 "
                "&& test ! -e /dev/disk0s1s1 && test ! -e /dev/disk0s1s2 && test ! -e /dev/disk0s2s1"
            )
        except ResetError:
            return False
        return True

    def _probe_model(self, expected=None):
        """Identify the connected hardware from the booted ramdisk.

        Prefer the kernel's own identifier: try sysctl at every common location
        and over ``PATH``, then ``uname -m``. The minimal Legacy iOS Kit 3.1.3
        SSH ramdisk used for the iPhone 3G timer flow ships none of these.

        When no kernel identifier is available, the live partition scheme is only
        used to *confirm* an already-expected iPhone 3G, never to guess a model
        from scratch. The flat S5L8900 layout is not unique to the 3G (an iPod
        touch 2G shares it), so without an expected model a bare-ramdisk device
        stays unidentified and inspect() fails closed. ``expected`` comes from the
        caller's ``--model``; on the ``prepare`` path the real board/chip (``n82ap``
        / S5L8900) was also verified during the kit DFU/iBSS boot. This is the
        inspection gate, not the hardware support proof.
        """
        for command in ("/usr/sbin/sysctl -n hw.machine", "/usr/sbin/sysctl -n hw.model",
                        "/sbin/sysctl -n hw.machine", "/sbin/sysctl -n hw.model",
                        "/usr/bin/sysctl -n hw.machine", "/usr/bin/sysctl -n hw.model",
                        "sysctl -n hw.machine", "sysctl -n hw.model",
                        "/usr/bin/uname -m", "uname -m"):
            try:
                answer = self.command(command).strip()
            except ResetError:
                continue
            if answer.startswith("iPhone"):
                return answer
            if answer.lower() in BOARD_MODELS:
                return BOARD_MODELS[answer.lower()]
        # No kernel identifier (the bare 3.1.3 ramdisk). Confirm an expected 3G
        # from the genuine flat layout only; the iOS 4 nested and iOS 5+ LwVM
        # layouts are rejected, and the version gate / HFS-mount checks in
        # inspect() backstop anything that still slips through.
        if expected == "iPhone1,2" and self._flat_3g_partition_layout():
            return "iPhone1,2"
        return None

    def inspect(self, mount=False, writable=False, expected_model=None):
        model = self._probe_model(expected_model)
        if model not in SUPPORTED:
            if model is None and expected_model is None and self._flat_3g_partition_layout():
                raise ResetError(
                    "This ramdisk reports no hardware model (no sysctl or uname). "
                    "For an iPhone 3G in a bare SSH ramdisk, run 'prepare' or pass "
                    "--model iPhone1,2."
                )
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
        disk_prefix = "/dev/disk0s" if model == "iPhone1,2" else "/dev/disk0s1s"
        def mounted_volumes(output):
            result = set()
            for number in ("1", "2"):
                found = re.search(r"^(\S+) on /mnt" + number + r" \(([^)]*)\)", output, re.MULTILINE)
                if found:
                    if found.group(1) != disk_prefix + number:
                        raise ResetError(f"Unexpected volume mounted at /mnt{number}.")
                    if "hfs" not in [part.strip() for part in found.group(2).split(",")]:
                        raise ResetError(f"/mnt{number} is not an HFS filesystem.")
                    result.add(number)
            return result

        volumes = mounted_volumes(mounts)
        mounted = len(volumes) == 2
        if not mounted and mount:
            # Use only the known HFS partitions, without mount.sh's repair
            # fallback for other disk layouts.
            self.command(f"test -b {disk_prefix}1 && test -b {disk_prefix}2")
            for number in ("1", "2"):
                if number not in volumes:
                    attributes = self.sftp.lstat("/mnt" + number)
                    if not stat.S_ISDIR(attributes.st_mode):
                        raise ResetError(f"/mnt{number} is not a regular mount directory.")
                    self.command(
                        f"/sbin/mount_hfs {disk_prefix}{number} /mnt{number}", timeout=60
                    )
            mounts = self.command("/sbin/mount")
            mounted = len(mounted_volumes(mounts)) == 2
        if not mounted:
            raise ResetError(
                "Expected legacy system/data volumes are not mounted. "
                "Retry with --mount to mount the known legacy HFS volumes. "
                "Filesystem repair is not performed automatically."
            )
        data_mount = re.search(r"^" + re.escape(disk_prefix) + r"2 on /mnt2 \(([^)]*)\)", mounts, re.MULTILINE)
        if writable and "read-only" in [part.strip() for part in data_mount.group(1).split(",")]:
            raise ResetError("The data volume is read-only; no transaction was started.")
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
            with channel_deadline(channel, IO_TIMEOUT):
                channel.exec_command(
                    "if test -x /sbin/reboot_bak; then /sbin/reboot_bak; else /sbin/reboot; fi"
                )
        finally:
            with suppress(Exception):
                channel.close()

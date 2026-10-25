"""Real SSH/SFTP over socketpair, with a simulated legacy filesystem.

This verifies the host transport and transaction, not an iPhone kernel, USB
hardware, pwnDFU, ramdisk compatibility, or the on-screen result.
"""

import errno
import io
import os
import plistlib
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import paramiko
from cryptography.hazmat.primitives import hashes

from ios_timeout import cli
from ios_timeout.core import JOURNAL, PREFS, SYSTEM_VERSION, ResetError
from ios_timeout.device import LOCK, USBDevice
from tests.test_core import encoded


class TestSFTP(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = root

    def local(self, path):
        candidate = self.root / path.lstrip("/")
        if ".." in Path(path).parts:
            raise PermissionError(path)
        return candidate

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).lstat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    stat = lstat

    def open(self, path, flags, attr):
        try:
            local = self.local(path)
            fd = os.open(str(local), flags, attr.st_mode or 0o600)
            if flags & os.O_RDWR:
                mode = "r+b"
            elif flags & os.O_WRONLY:
                mode = "wb"
            else:
                mode = "rb"
            stream = os.fdopen(fd, mode, buffering=0)
            handle = paramiko.SFTPHandle(flags)
            handle.filename = str(local)
            handle.readfile = stream
            handle.writefile = stream
            handle.chattr = lambda attributes: self.chattr(path, attributes)
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def mkdir(self, path, attr):
        try:
            self.local(path).mkdir(mode=attr.st_mode or 0o700)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rmdir(self, path):
        try:
            self.local(path).rmdir()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def symlink(self, target_path, path):
        try:
            os.symlink(target_path, self.local(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def readlink(self, path):
        try:
            return os.readlink(self.local(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            self.local(path).unlink()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, source, destination):
        try:
            if self.local(destination).exists():
                return paramiko.SFTP_FAILURE
            self.local(source).rename(self.local(destination))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def posix_rename(self, source, destination):
        # Older ramdisks may lack the OpenSSH extension: exercise mv fallback.
        return paramiko.SFTP_OP_UNSUPPORTED

    def chattr(self, path, attr):
        try:
            paramiko.SFTPServer.set_file_attr(str(self.local(path)), attr)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class Server(paramiko.ServerInterface):
    def __init__(self, root, owner):
        self.root = root
        self.owner = owner

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == ("root", "alpine") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.owner.channels.append(channel)
        def work():
            status, output = 0, ""
            try:
                args = shlex.split(command.decode())
                if (args[:1] in (["/usr/sbin/sysctl"], ["/sbin/sysctl"],
                                 ["/usr/bin/sysctl"], ["sysctl"]) and args[1:2] == ["-n"]):
                    if self.owner.kernel_query:
                        output = self.owner.model + "\n"
                    else:
                        status, output = 1, ""  # no sysctl on the bare 3.1.3 ramdisk
                elif args in (["/usr/bin/uname", "-m"], ["uname", "-m"]):
                    if self.owner.kernel_query:
                        output = self.owner.model + "\n"
                    else:
                        status, output = 127, ""  # no uname on the bare 3.1.3 ramdisk
                elif args == ["/sbin/mount"]:
                    root = "/dev/md0" if self.owner.ramdisk else "/dev/disk0s1s1"
                    output = root + " on / (hfs, local)\n"
                    if self.owner.mounted:
                        options = "hfs, local, read-only" if self.owner.read_only else "hfs, local"
                        prefix = "/dev/disk0s" if self.owner.model == "iPhone1,2" else "/dev/disk0s1s"
                        output += f"{prefix}1 on /mnt1 (hfs, local)\n{prefix}2 on /mnt2 ({options})\n"
                elif command.decode().startswith("if test -x /bin/ps;"):
                    output = "/sbin/launchd\n/usr/sbin/sshd\n"
                    if self.owner.springboard:
                        output += "/System/Library/CoreServices/SpringBoard.app/SpringBoard\n"
                elif args[:1] == ["/sbin/mount_hfs"]:
                    self.owner.mounted = True
                elif command.decode().startswith("test "):
                    # Evaluate any && chain of `test -b PATH` / `test ! -e PATH`
                    # against the device's real set of present block nodes, so
                    # the partition-scheme probe is exercised for what it is.
                    nodes = (self.owner.block_nodes if self.owner.block_nodes is not None
                             else ({"/dev/disk0s1", "/dev/disk0s2"} if self.owner.model == "iPhone1,2"
                                   else {"/dev/disk0s1", "/dev/disk0s1s1", "/dev/disk0s1s2"}))
                    for clause in command.decode().split(" && "):
                        parts = clause.split()
                        if parts[:2] == ["test", "-b"]:
                            satisfied = parts[2] in nodes
                        elif parts[:3] == ["test", "!", "-e"]:
                            satisfied = parts[3] not in nodes
                        else:
                            satisfied = False
                        if not satisfied:
                            status = 1
                            break
                elif args[:2] == ["/bin/cp", "-p"]:
                    shutil.copy2(self.root / args[2].lstrip("/"), self.root / args[3].lstrip("/"))
                elif args[:2] == ["/bin/mv", "-f"]:
                    os.replace(self.root / args[2].lstrip("/"), self.root / args[3].lstrip("/"))
                elif args == ["/bin/sync"]:
                    pass
                else:
                    status, output = 127, "Unimplemented test command"
                if output:
                    channel.sendall(output.encode())
                channel.send_exit_status(status)
            finally:
                channel.shutdown_write()
        threading.Thread(target=work, daemon=True).start()
        return True


@unittest.skipIf(sys.platform == "win32", "Server fixture requires POSIX ownership and symlink semantics")
class SSHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "phone"
        self.state = Path(self.temp.name) / "state"
        self.backups = Path(self.temp.name) / "backups"
        self.model = "iPhone5,2"
        self.ramdisk = True
        self.kernel_query = True
        self.block_nodes = None  # None -> derive block devices from the model
        self.springboard = False
        self.mounted = True
        self.read_only = False
        self.legacy_hostkey = False
        self.selected = {"udid": "e" * 40, "device_id": 9}
        for path, data in (
            (PREFS, encoded()),
            (JOURNAL, encoded({"SBDeviceLockFailedAttempts": 11})),
            (SYSTEM_VERSION, plistlib.dumps({"ProductVersion": "9.3.5", "ProductBuildVersion": "13G36"})),
        ):
            local = self.root / path.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            local.chmod(0o600)
        self.servers = []
        self.channels = []
        self.addCleanup(self.close_servers)

    def close_servers(self):
        for server in self.servers:
            server.close()
        for channel in self.channels:
            try:
                channel.close()
            except (EOFError, OSError):
                pass

    def connect(self, *_args, **_kwargs):
        client, server_socket = socket.socketpair()
        transport = paramiko.Transport(server_socket)
        key = self.key
        if self.legacy_hostkey:
            class LegacyServerKey(paramiko.RSAKey):
                HASHES = {**paramiko.RSAKey.HASHES, "ssh-rsa": hashes.SHA1}
            key = LegacyServerKey(key=self.key.key)
            transport._key_info = {**transport._key_info, "ssh-rsa": LegacyServerKey}
            transport.get_security_options().key_types = ("ssh-rsa",)
        transport.add_server_key(key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, TestSFTP, root=self.root)
        self.servers.append(transport)
        thread = threading.Thread(
            target=lambda: transport.start_server(server=Server(self.root, self)), daemon=True)
        thread.start()
        return client

    def device(self, **kwargs):
        with patch("ios_timeout.usbmux.tunnel", side_effect=self.connect):
            return USBDevice(self.selected, self.state, "alpine", **kwargs)

    def test_full_cli_preview_commit_and_restore_over_ssh(self):
        original = (self.root / PREFS.lstrip("/")).read_bytes()
        arguments = ["--state-dir", str(self.state)]
        output = io.StringIO()
        with patch("ios_timeout.usbmux.tunnel", side_effect=self.connect), \
             patch("ios_timeout.usbmux.list_devices", return_value=[self.selected]), \
             patch("ios_timeout.macos_agents.recovery_agents", lambda: nullcontext()), \
             redirect_stdout(output):
            self.assertEqual(cli.main(["inspect"] + arguments), 0)
            self.assertEqual((self.root / PREFS.lstrip("/")).read_bytes(), original)
            self.assertFalse(self.backups.exists())
            self.assertEqual(cli.main(["reset", "--apply", "--backups", str(self.backups)] + arguments), 0)
            folder = next(self.backups.iterdir())
            data = plistlib.loads((self.root / PREFS.lstrip("/")).read_bytes())
            self.assertEqual(data["SBDeviceLockFailedAttempts"], 0)
            self.assertFalse((self.root / JOURNAL.lstrip("/")).exists())
            self.assertEqual(cli.main(["restore", str(folder), "--apply"] + arguments), 0)
        self.assertEqual((self.root / PREFS.lstrip("/")).read_bytes(), original)
        self.assertTrue((self.root / JOURNAL.lstrip("/")).exists())
        self.assertIn("read-back verified", output.getvalue())

    def test_modern_device_is_rejected_before_mount(self):
        self.model = "iPhone10,1"
        self.mounted = False
        with self.device() as device, self.assertRaises(ResetError):
            device.inspect(mount=True)
        self.assertFalse(self.mounted)

    def test_live_ios_is_rejected(self):
        self.ramdisk = False
        with self.device() as device, self.assertRaises(ResetError):
            device.inspect()

    def test_running_springboard_is_rejected(self):
        self.springboard = True
        with self.device() as device, self.assertRaises(ResetError):
            device.inspect()

    def test_mount_is_explicit(self):
        self.mounted = False
        with self.device() as device:
            with self.assertRaises(ResetError):
                device.inspect()
            self.assertEqual(device.inspect(mount=True).version, "9.3.5")

    def test_3g_ios4_uses_old_partition_layout_and_recovers(self):
        self.model = "iPhone1,2"
        self.mounted = False
        version = self.root / SYSTEM_VERSION.lstrip("/")
        version.write_bytes(plistlib.dumps({"ProductVersion": "4.2.1", "ProductBuildVersion": "8C148"}))
        from ios_timeout.core import make_plan, apply_plan, restore_backup
        original = (self.root / PREFS.lstrip("/")).read_bytes()
        with self.device() as device:
            identity = device.inspect(mount=True, writable=True)
            self.assertEqual(identity.model, "iPhone1,2")
            folder = apply_plan(device, make_plan(device, identity), self.backups)
            restore_backup(device, identity, folder)
        self.assertEqual((self.root / PREFS.lstrip("/")).read_bytes(), original)

    def test_3g_without_sysctl_is_identified_from_the_partition_layout(self):
        # The Legacy iOS Kit 3.1.3 SSH ramdisk used by the timer flow ships no
        # sysctl or uname, so hw.machine cannot be read. The genuine flat S5L8900
        # partition scheme still identifies the iPhone 3G live on the ramdisk.
        # This reproduces the logged "Unsupported or unidentifiable hardware:
        # None" failure and confirms recovery through a full preview->commit->restore.
        self.model = "iPhone1,2"
        self.kernel_query = False
        version = self.root / SYSTEM_VERSION.lstrip("/")
        version.write_bytes(plistlib.dumps({"ProductVersion": "4.2.1", "ProductBuildVersion": "8C148"}))
        from ios_timeout.core import make_plan, apply_plan, restore_backup
        original = (self.root / PREFS.lstrip("/")).read_bytes()
        with self.device() as device:
            identity = device.inspect(mount=True, writable=True, expected_model="iPhone1,2")
            self.assertEqual(identity.model, "iPhone1,2")
            self.assertEqual(identity.version, "4.2.1")
            folder = apply_plan(device, make_plan(device, identity), self.backups)
            restore_backup(device, identity, folder)
        self.assertEqual((self.root / PREFS.lstrip("/")).read_bytes(), original)

    def test_flat_layout_without_an_expected_3g_fails_closed(self):
        # The flat S5L8900 layout is not unique to the 3G (an iPod touch 2G
        # shares it). With no kernel query AND no expected iPhone1,2, the fallback
        # must NOT guess a model: it fails closed with a message pointing at
        # prepare / --model, rather than silently accepting a non-3G device.
        self.model = "iPhone1,2"
        self.kernel_query = False
        with self.device() as device, \
             self.assertRaisesRegex(ResetError, "reports no hardware model"):
            device.inspect(mount=True)

    def test_flat_layout_with_a_non_3g_expected_model_fails_closed(self):
        # The flat-layout fallback only ever confirms iPhone1,2; expecting any
        # other model on a kernel-query-less ramdisk still fails closed.
        self.model = "iPhone1,2"
        self.kernel_query = False
        with self.device() as device, \
             self.assertRaisesRegex(ResetError, "Unsupported or unidentifiable hardware"):
            device.inspect(mount=True, expected_model="iPhone5,2")

    def test_ios4_nested_data_layout_is_not_taken_for_a_3g(self):
        # The iOS 4 nested scheme (data at /dev/disk0s2s1, with /dev/disk0s2 as a
        # block container) must NOT be confirmed as the flat iPhone 3G layout,
        # even when an iPhone1,2 is expected. Otherwise a 3GS/iPhone 4 on iOS 4
        # would be mislabeled iPhone1,2 instead of failing closed.
        self.kernel_query = False
        self.block_nodes = {"/dev/disk0s1", "/dev/disk0s2", "/dev/disk0s2s1"}
        with self.device() as device, \
             self.assertRaisesRegex(ResetError, "Unsupported or unidentifiable hardware"):
            device.inspect(mount=True, expected_model="iPhone1,2")

    def test_unreadable_model_without_old_layout_is_rejected(self):
        # A ramdisk that serves no kernel query and is not the flat 3G layout
        # (here the iOS 5+ LwVM scheme) must still fail closed, even when a 3G is
        # expected, not guess a model.
        self.kernel_query = False
        with self.device() as device, \
             self.assertRaisesRegex(ResetError, "Unsupported or unidentifiable hardware"):
            device.inspect(mount=True, expected_model="iPhone1,2")

    def test_usb_connection_retries_through_a_hub_reenumeration_bounce(self):
        # On Apple Silicon a 30-pin phone reaches the Mac through a USB-C dock,
        # which can briefly drop the phone off usbmuxd during the handshake.
        # The connection retries (re-resolving the serial) instead of failing.
        attempts = []

        def flaky(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise ResetError("No matching USB iPhone is exposed by usbmuxd.")
            return self.connect(*args, **kwargs)

        with patch("ios_timeout.usbmux.tunnel", side_effect=flaky), \
             patch("ios_timeout.device.time.sleep", lambda _s: None):
            device = USBDevice(self.selected, self.state, "alpine")
            self.addCleanup(device.close)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(device.inspect().model, self.model)

    def test_symlink_target_is_rejected(self):
        prefs = self.root / PREFS.lstrip("/")
        prefs.rename(prefs.with_suffix(".saved"))
        prefs.symlink_to(prefs.with_suffix(".saved"))
        with self.device() as device, self.assertRaises(ResetError):
            device.read(PREFS)

    def test_host_key_change_requires_explicit_option(self):
        with self.device():
            pass
        self.key = paramiko.RSAKey.generate(2048)
        with self.assertRaises(ResetError):
            self.device()
        with self.device(trust_new_key=True):
            pass

    def test_legacy_rsa_server_authenticates_and_preserves_host_key_pinning(self):
        self.legacy_hostkey = True
        preferred = paramiko.Transport._preferred_keys
        implementations = dict(paramiko.Transport._key_info)
        hash_algorithms = dict(paramiko.RSAKey.HASHES)
        with self.device() as device:
            self.assertEqual(device.transport.host_key_type, "ssh-rsa")
            self.assertEqual(device.read(PREFS)[0], encoded())
        self.key = paramiko.RSAKey.generate(2048)
        with self.assertRaisesRegex(ResetError, "host key changed"):
            self.device()
        self.assertEqual(paramiko.Transport._preferred_keys, preferred)
        self.assertEqual(paramiko.Transport._key_info, implementations)
        self.assertEqual(paramiko.RSAKey.HASHES, hash_algorithms)

    def test_modern_server_keeps_sha2_preference(self):
        with self.device() as device:
            self.assertTrue(device.transport.host_key_type.startswith("rsa-sha2-"))

    def test_atomic_lock_carries_owner_and_releases_idempotently(self):
        token = "f" * 32
        with self.device() as device:
            device.acquire(token)
            lock = self.root / LOCK.lstrip("/")
            self.assertTrue(lock.is_symlink())
            self.assertEqual(os.readlink(lock), token)
            with self.assertRaises(ResetError):
                device.acquire("a" * 32)
            device.acquire(token, recover=True)
            device.release(token)
            device.release(token)
            self.assertFalse(lock.is_symlink())

    def test_version_one_directory_lock_can_be_recovered(self):
        token = "f" * 32
        lock = self.root / LOCK.lstrip("/")
        lock.mkdir()
        (lock / "owner").write_text(token)
        with self.device() as device:
            device.acquire(token, recover=True)
            device.release(token)
        self.assertFalse(lock.exists())

    def test_read_only_volume_can_be_inspected_but_not_written(self):
        self.read_only = True
        with self.device() as device:
            self.assertEqual(device.inspect().model, self.model)
            with self.assertRaisesRegex(ResetError, "read-only"):
                device.inspect(writable=True)
        self.assertFalse(self.backups.exists())

    def test_symlink_parent_is_rejected(self):
        folder = self.root / Path(PREFS.lstrip("/")).parent
        saved = folder.with_name("SavedPreferences")
        folder.rename(saved)
        folder.symlink_to(saved, target_is_directory=True)
        with self.device() as device, self.assertRaises(ResetError):
            device.read(PREFS)


if __name__ == "__main__":
    unittest.main()

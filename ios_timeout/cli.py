import argparse
import fcntl
import getpass
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from . import APP_NAME, __version__, usbmux
from .core import (
    ResetError, SUPPORTED, apply_plan, digest, finalize_backup, load_backup, make_plan, restore_backup,
)
from .device import USBDevice

DEFAULT_STATE = (
    Path.home() / f"Library/Application Support/{APP_NAME}"
    if sys.platform == "darwin" else
    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "bruteforce-legacy-ios"
)
KIT_REPO = "https://github.com/LukeZGD/Legacy-iOS-Kit.git"
KIT_COMMIT = "23622f8fe72c09952089d5145a7b008cfcc90d9b"


@contextmanager
def host_lock(state_dir):
    if state_dir.is_symlink():
        raise ResetError("The state directory must not be a symlink.")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_dir.stat().st_uid != os.getuid():
        raise ResetError("The state directory must belong to the current user.")
    state_dir.chmod(0o700)
    fd = os.open(str(state_dir / "session.lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        attributes = os.fstat(fd)
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != os.getuid():
            raise ResetError("Invalid local session lock.")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResetError("Another instance is already using this state directory.") from exc
        yield
    finally:
        os.close(fd)


def port_number(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parser():
    result = argparse.ArgumentParser(
        prog="bruteforce-legacy-ios",
        description="One-time lockout-state reset for iPhone 4/4S/5/5c via USB SSH ramdisk.",
        epilog="Reset commands preview changes unless --apply is supplied.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="action", required=True)
    commands.add_parser("devices", help="List USB devices exposed by usbmuxd (read-only).")
    commands.add_parser("doctor", help="Check this Mac and show supported model ranges.")
    commands.add_parser("wizard", help="Interactive menu, also used by Launch.command.")
    for name, help_text in (
        ("inspect", "Read device identity and preview the timer reset."),
        ("reset", "Preview or apply reset to a booted ramdisk."),
        ("prepare", "Launch Legacy iOS Kit's ramdisk flow, then preview/apply reset."),
        ("restore", "Recover original files from a reset transaction."),
        ("finalize", "Verify a completed reset/restore and finish lock cleanup."),
    ):
        item = commands.add_parser(name, help=help_text)
        item.add_argument("--udid", help="Select a specific USB device.")
        item.add_argument("--port", type=port_number, default=22, help="Device SSH port (default 22).")
        item.add_argument("--mount", action="store_true", help="Mount known legacy HFS volumes if needed.")
        item.add_argument("--ask-password", action="store_true",
                          help="Prompt for a custom ramdisk SSH password instead of alpine.")
        item.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        item.add_argument("--trust-new-key", action="store_true",
                          help="Replace the selected USB phone's saved SSH host key.")
        if name != "inspect":
            item.add_argument("--apply", action="store_true", help="Write the reviewed changes.")
            item.add_argument("--reboot", action="store_true",
                              help="Request reboot after a verified commit/restore.")
        if name in ("reset", "prepare"):
            item.add_argument("--backups", type=Path, help="Backup directory (default: STATE_DIR/backups).")
        if name == "prepare":
            item.add_argument("--kit", type=Path,
                              help="Legacy-iOS-Kit directory; a pinned copy is downloaded if absent.")
        if name in ("restore", "finalize"):
            item.add_argument("backup", type=Path, help="Transaction directory containing manifest.json.")
    return result


def doctor():
    print(f"{APP_NAME} {__version__}")
    print(f"Host: {platform.system()} {platform.release()} / {platform.machine()}")
    print(f"Python: {platform.python_version()}")
    print(f"Default state/backups: {DEFAULT_STATE}")
    print(f"usbmuxd socket: {'found' if Path(usbmux.SOCKET_PATH).exists() else 'missing'}")
    print(f"git: {'found' if shutil.which('git') else 'not installed'} (only needed to obtain a backend)")
    try:
        import paramiko
        print(f"SSH library: Paramiko {paramiko.__version__}")
    except ImportError:
        print("SSH library: missing; run ./Launch.command or python3 -m pip install .")
    print("\nAccepted legacy model/version ranges (hardware validation still required):")
    for model, (name, low, high) in SUPPORTED.items():
        print(f"  {model:10} {name:10} iOS {'.'.join(map(str, low))}–{'.'.join(map(str, high))}")
    print("\niPhone 4S pwnDFU requires external A5 hardware or a suitable existing kDFU setup.")
    print("This release has not been validated on a physical phone.")


def describe(plan):
    device = plan.identity
    print(f"\n{SUPPORTED[device.model][0]} · {device.model} · iOS {device.version} ({device.build})")
    print(f"USB identifier: {device.udid}")
    for note in plan.notes:
        print("  " + note)
    if not plan.changes:
        print("\nRecognized state is already reset; no file changes are needed.")
    for change in plan.changes:
        action = "archive journal" if change.after is None else "update plist"
        print(f"\n  {action}: {change.path}")
        print(f"  original SHA-256: {digest(change.before)}")


def launch_backend(args):
    if platform.system() != "Darwin":
        raise ResetError("The integrated prepare workflow is supported on macOS only.")
    kit = (args.kit or args.state_dir / "backends/Legacy-iOS-Kit").expanduser().resolve()
    if not kit.exists():
        if not shutil.which("git"):
            raise ResetError("git is required to download Legacy iOS Kit; alternatively supply --kit.")
        kit.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading official Legacy iOS Kit at {KIT_COMMIT}...", flush=True)
        with tempfile.TemporaryDirectory(prefix=".itr-backend-", dir=str(kit.parent)) as staging:
            checkout = Path(staging) / "Legacy-iOS-Kit"
            for command in (
                ["git", "clone", "--filter=blob:none", "--no-checkout", KIT_REPO, str(checkout)],
                ["git", "-C", str(checkout), "checkout", "--detach", KIT_COMMIT],
            ):
                completed = subprocess.run(
                    command, check=False, timeout=900,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                if completed.returncode:
                    raise ResetError("Backend download failed. No phone changes were attempted.")
            actual = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                                    check=True, capture_output=True, text=True, timeout=30).stdout.strip()
            if actual != KIT_COMMIT:
                raise ResetError("Downloaded backend revision did not match the pinned commit.")
            checkout.rename(kit)
    script = kit / "restore.sh"
    if not script.is_file() or not (kit / "resources").is_dir():
        raise ResetError("--kit must point to an extracted Legacy iOS Kit directory.")
    print("\nOpening Legacy iOS Kit's SSH-ramdisk workflow.", flush=True)
    print("Follow its DFU/ramdisk prompts. At the SSH Ramdisk Menu, choose Exit.", flush=True)
    print("Connect only one phone for this backend step; --udid selects the phone after boot.", flush=True)
    print("Leave the phone in the ramdisk; the reset tool resumes when the backend exits.", flush=True)
    print(f"Backend: {script}\nSHA-256: {digest(script.read_bytes())}", flush=True)
    completed = subprocess.run(
        ["bash", str(script), "--sshrd", "--no-version-check", "--no-color"],
        cwd=str(kit), check=False,
    )
    if completed.returncode:
        raise ResetError(f"Legacy iOS Kit exited with status {completed.returncode}. No reset was attempted.")
    args.mount = True


def wizard():
    print(f"\n{APP_NAME} {__version__}")
    print("For iPhone 4 / 4S / 5 / 5c, with a known passcode.")
    print("1. Check Mac and compatibility")
    print("2. Inspect a phone already in an SSH ramdisk")
    print("3. Reset timer on a phone already in an SSH ramdisk")
    print("4. Start with Legacy iOS Kit, then reset timer")
    print("5. Restore an earlier transaction")
    print("6. Finish interrupted cleanup")
    print("0. Exit")
    choice = input("\nChoose: ").strip()
    if choice == "0":
        return 0
    if choice == "1":
        return main(["doctor"])
    if choice not in ("2", "3", "4", "5", "6"):
        raise ResetError("Choose a number from the menu.")
    action = {"2": "inspect", "3": "reset", "4": "prepare", "5": "restore", "6": "finalize"}[choice]
    command = [action, "--mount"]
    if action == "prepare":
        kit = input("Existing Legacy iOS Kit folder (Return to download a pinned copy): ").strip().strip("'\"")
        if kit:
            command.extend(["--kit", kit])
    if action in ("restore", "finalize"):
        folder = input("Backup transaction folder: ").strip().strip("'\"")
        command.append(folder)
    if action != "inspect":
        command.append("--apply")
    return main(command)


def run(args):
    if args.action == "doctor":
        doctor()
        return 0
    if args.action == "devices":
        devices = usbmux.list_devices()
        print(json.dumps(devices, indent=2))
        if not devices:
            print("DFU/recovery devices are not listed by usbmuxd; boot the SSH ramdisk first.")
        return 0
    if args.action == "wizard":
        return wizard()
    if getattr(args, "reboot", False) and not args.apply:
        raise ResetError("--reboot requires --apply.")
    if args.action in ("restore", "finalize"):
        # Validate every local backup before opening a device connection.
        _, saved, identity, _ = load_backup(args.backup)
        if args.udid is None:
            args.udid = identity.udid
    state = args.state_dir.expanduser().absolute()
    args.state_dir = state
    if args.action in ("reset", "prepare") and args.backups is None:
        args.backups = state / "backups"
    with host_lock(state):
        if args.action == "prepare":
            launch_backend(args)
        selected = usbmux.select_device(usbmux.list_devices(), args.udid)
        password = (getpass.getpass("Ramdisk SSH root password: ")
                    if args.ask_password else "alpine")
        with USBDevice(selected, state, password, args.port, args.trust_new_key) as device:
            print(f"USB SSH fingerprint: {device.fingerprint}")
            identity = device.inspect(mount=args.mount, writable=getattr(args, "apply", False))
            if args.action in ("restore", "finalize"):
                if asdict(identity) != saved["identity"]:
                    raise ResetError("Backup identity does not match the connected phone.")
                operation = "Restore originals" if args.action == "restore" else "Finalize verified transaction"
                print(f"{operation} for {identity.model} / iOS {identity.version}")
                if not args.apply:
                    print(f"Preview only. Add --apply to {args.action} the saved transaction.")
                    return 0
                if args.action == "restore":
                    folder = restore_backup(device, identity, args.backup)
                    print(f"Original files restored and verified. Backup: {folder}")
                else:
                    folder = finalize_backup(device, identity, args.backup)
                    print(f"Transaction verified and lock cleanup complete. Backup: {folder}")
            else:
                plan = make_plan(device, identity)
                describe(plan)
                if args.action == "inspect" or not args.apply:
                    print("\nPreview only. No lockout files were changed.")
                    return 0
                folder = apply_plan(
                    device, plan, args.backups,
                    on_backup=lambda path: print(f"\nBackup ready: {path}", flush=True),
                )
                if folder:
                    print(f"\nPlist reset committed and read-back verified. Backup: {folder}")
                    print("On-screen timer clearance remains unverified until the phone boots.")
                else:
                    print("\nNo changes made.")
            if getattr(args, "reboot", False):
                device.reboot()
                print("Reboot request sent. Enter the correct passcode when the phone starts.")
            else:
                print("Phone left in its ramdisk. Reboot using the ramdisk tool when ready.")
    return 0


def main(argv=None):
    try:
        return run(parser().parse_args(argv))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (ResetError, OSError, ValueError, EOFError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Paramiko errors are optional imports; keep authentication/disconnect
        # failures concise without requiring the dependency for doctor/tests.
        print(f"\nConnection or operation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

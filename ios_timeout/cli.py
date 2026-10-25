import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from . import APP_NAME, __version__, usbmux
from .core import (
    ResetError, SUPPORTED, apply_plan, digest, finalize_backup, load_backup, make_plan, restore_backup,
)
from .device import USBDevice
from .host import host_lock, state_directory
from .backend_support import PHONES

DEFAULT_STATE = state_directory()
KIT_REPO = "https://github.com/LukeZGD/Legacy-iOS-Kit.git"
KIT_COMMIT = "23622f8fe72c09952089d5145a7b008cfcc90d9b"


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
        prog="openup",
        description="Legacy iPhone timer reset and integrated passcode-recovery ramdisk tools.",
        epilog="Reset commands preview changes unless --apply is supplied.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="action", required=True)
    devices_command = commands.add_parser("devices", help="List USB devices exposed by usbmuxd (read-only).")
    devices_command.add_argument("--wsl", action="store_true", help="Windows: list devices inside WSL.")
    devices_command.add_argument("--wsl-distro")
    commands.add_parser("doctor", help="Check this computer and show platform requirements.")
    commands.add_parser("wizard", help="Interactive menu, also used by Launch.command.")
    update = commands.add_parser("update", help="Check/install a verified GitHub release, or roll back.")
    update_action = update.add_mutually_exclusive_group()
    update_action.add_argument("--apply", action="store_true", help="Install a newer verified release.")
    update.add_argument("--prerelease", action="store_true", help="Include prereleases.")
    update_action.add_argument("--rollback", action="store_true", help="Reactivate the previous installation.")
    update.set_defaults(state_dir=DEFAULT_STATE)
    recover = commands.add_parser("recover", help="Run the pinned passcode-recovery backend.")
    recover.add_argument("--apply", action="store_true", help="Run the backend and boot its ramdisk.")
    recover.add_argument("--install-only", action="store_true", help="Download and check the backend without device access.")
    recover.add_argument("--backend-dir", type=Path)
    recover.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    recover.add_argument("--model", choices=sorted(PHONES))
    recover.add_argument("--armv6-tools", type=Path, help="Optional custom ARMv6 tools folder; bundled iPhone 3G tools are used by default.")
    recover.add_argument("--wsl-distro", help="Windows: select a WSL 2 distribution.")
    for name, help_text in (
        ("inspect", "Read device identity and preview the timer reset."),
        ("reset", "Preview or apply reset to a booted ramdisk."),
        ("prepare", "Launch Legacy iOS Kit's ramdisk flow, then preview/apply reset."),
        ("restore", "Recover original files from a reset transaction."),
        ("finalize", "Verify a completed reset/restore and finish lock cleanup."),
    ):
        item = commands.add_parser(name, help=help_text)
        item.add_argument("--udid", help="Select a specific USB device.")
        item.add_argument("--model", choices=sorted(SUPPORTED),
                          help="Expected hardware model; confirms an iPhone 3G (iPhone1,2) in a "
                               "bare SSH ramdisk that reports no sysctl. 3G is iPhone1,2.")
        item.add_argument("--port", type=port_number, default=22, help="Device SSH port (default 22).")
        item.add_argument("--mount", action="store_true", help="Mount known legacy HFS volumes if needed.")
        item.add_argument("--ask-password", action="store_true",
                          help="Prompt for a custom ramdisk SSH password instead of alpine.")
        item.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        item.add_argument("--wsl", action="store_true", help="Windows: run this device operation inside WSL.")
        item.add_argument("--wsl-distro", help="Windows: select a WSL 2 distribution.")
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
            item.add_argument("--ipsw", type=Path, help="Optional local iPhone 3G 3.1.3/7E18 boot IPSW.")
        if name in ("restore", "finalize"):
            item.add_argument("backup", type=Path, help="Transaction directory containing manifest.json.")
    return result


def doctor():
    print(f"{APP_NAME} {__version__}")
    print(f"Host: {platform.system()} {platform.release()} / {platform.machine()}")
    print(f"Python: {platform.python_version()}")
    print(f"Default state/backups: {DEFAULT_STATE}")
    print(f"USB transport endpoint: {usbmux.default_endpoint()}")
    print(f"git: {'found' if shutil.which('git') else 'not installed'} (only needed to obtain a backend)")
    try:
        import paramiko
        print(f"SSH library: Paramiko {paramiko.__version__}")
    except ImportError:
        print("SSH library: missing; run ./Launch.command or python3 -m pip install .")
    if sys.platform == "win32":
        print("Native USB SSH: Apple Mobile Device Service is required.")
        print(f"Ramdisk preparation: WSL 2 ({'found' if shutil.which('wsl.exe') else 'missing'}) "
              f"and usbipd-win ({'found' if shutil.which('usbipd.exe') else 'missing'}) are required.")
        print("Share/attach the phone to WSL with usbipd before prepare or recover.")
    elif sys.platform.startswith("linux"):
        print("Install/start usbmuxd and ensure USB device permissions (udev/sudo).")
    print("\nTimer-reset model/version gates (hardware validation still required):")
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
    if platform.system() not in ("Darwin", "Linux"):
        raise ResetError("Use WSL 2 for Windows ramdisk preparation.")
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
    if args.ipsw:
        if args.model != "iPhone1,2":
            raise ResetError("--ipsw currently requires --model iPhone1,2 and the 3.1.3/7E18 boot IPSW.")
        from .firmware import validate_ipsw
        validate_ipsw(args.ipsw, args.model, "7E18")
        destination = kit / "iPhone1,2_3.1.3_7E18_Restore.ipsw"
        source = args.ipsw.expanduser().resolve()
        if destination.exists():
            validate_ipsw(destination, args.model, "7E18")
        else:
            # Keep user-supplied firmware out of the application release.
            with tempfile.TemporaryDirectory(prefix=".ipsw-", dir=kit) as temporary:
                staged = Path(temporary) / destination.name
                shutil.copyfile(source, staged)
                validate_ipsw(staged, args.model, "7E18")
                staged.rename(destination)
    extra = []
    if platform.system() == "Darwin":
        # Openup's device-agent guardian owns suspending and restoring Apple's
        # AMPDevices agents across the whole operation. Tell the kit not to
        # resume them when it exits, so they cannot grab the phone during the
        # handoff to the ramdisk SSH session (this reliably fails on Intel Macs
        # otherwise). The guardian restores exactly the agents it paused.
        extra.append("--no-finder")
    if os.environ.get("BL_IOS_UI_PROTOCOL") == "1":
        # The GUI renders numbered menus as buttons. The ANSI menu requests
        # terminal cursor coordinates, which a pipe/PTY bridge cannot supply.
        extra.append("--old-menu")
    if args.model:
        extra.append("--device=" + args.model)
    if args.model == "iPhone1,2":
        from .firmware import seed_3g_url
        seed_3g_url(kit, managed=args.kit is None)
        extra.append("--build-id=7E18")
        print("3G target: iPhone1,2 / n82ap; 3.1.3 boot ramdisk. Installed iOS is unchanged.", flush=True)
    print("\nOpening Legacy iOS Kit's SSH-ramdisk workflow.", flush=True)
    print("Follow its on-screen DFU/ramdisk steps. The SSH Ramdisk Menu is left automatically.", flush=True)
    print("Connect only one phone for this backend step; --udid selects the phone after boot.", flush=True)
    print("The phone stays in the ramdisk; the reset tool resumes when the backend exits.", flush=True)
    print(f"Backend: {script}\nSHA-256: {digest(script.read_bytes())}", flush=True)
    original = script.read_bytes()
    from .legacy_kit import prepared_script, prepared_menu_script, script_is_pinned
    if args.model == "iPhone1,2":
        # Full 3G adaptation: identity checks plus shared GUI prompt/menu handling.
        prepared = prepared_script(kit, original)
    elif script_is_pinned(original):
        # Other supported models: keep the whole timer workflow in the GUI by
        # applying the shared prompt hooks and non-interactive ramdisk menu,
        # without the 3G-specific identity checks.
        prepared = prepared_menu_script(kit, original)
    else:
        # A user-supplied, non-pinned checkout is run unchanged.
        prepared = nullcontext(script)
    with prepared as running_script:
        env = {**os.environ, "BL_IOS_PYTHON": sys.executable,
               # Boot the ramdisk and return without the interactive SSH Ramdisk
               # Menu; Openup's own reset step resumes once the backend exits.
               "BL_IOS_SSHRD_AUTOEXIT": "1",
               "PYTHONPATH": str(Path(__file__).resolve().parent.parent) +
               (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
        if running_script != script:
            subprocess.run(["bash", "-n", str(running_script)], check=True, timeout=30)
            note = ("Applied reviewed 3G identity checks and GUI menu handling"
                    if args.model == "iPhone1,2"
                    else "Applied reviewed GUI menu handling")
            print(note + "; original backend files are unchanged.", flush=True)
        completed = subprocess.run(
            ["bash", str(running_script), "--sshrd", "--no-version-check", "--no-color"] + extra,
            cwd=str(kit), env=env, check=False,
        )
    if completed.returncode:
        raise ResetError(f"Legacy iOS Kit exited with status {completed.returncode}. No reset was attempted.")
    args.mount = True


def wizard():
    print(f"\n{APP_NAME} {__version__}")
    print("Legacy iPhone recovery tools")
    print("1. Check computer and compatibility")
    print("2. Inspect a phone already in an SSH ramdisk")
    print("3. Reset timer on a phone already in an SSH ramdisk")
    print("4. Start with Legacy iOS Kit, then reset timer")
    print("5. Restore an earlier transaction")
    print("6. Finish interrupted cleanup")
    print("7. Start passcode recovery")
    print("8. Prepare and reset an iPhone 3G on iOS 4")
    print("9. Check / install updates / roll back")
    print("0. Exit")
    choice = input("\nChoose: ").strip()
    if choice == "0":
        return 0
    if choice == "1":
        return main(["doctor"])
    if choice == "7":
        return main(["recover", "--apply"])
    if choice == "8":
        return main(["prepare", "--model", "iPhone1,2", "--apply"])
    if choice == "9":
        action = input("Update: [c]heck, [i]nstall, [r]oll back (default c): ").strip().lower()
        if action == "r":
            return main(["update", "--rollback"])
        if action not in ("", "c", "i"):
            raise ResetError("Choose c, i, or r.")
        return main(["update", "--prerelease"] + (["--apply"] if action == "i" else []))
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
    if args.action == "update":
        from .updates import run as update
        return update(args)
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
    if args.action == "recover":
        from .upstream import run_backend
        args.state_dir = args.state_dir.expanduser().absolute()
        with host_lock(args.state_dir):
            return run_backend(args)
    if getattr(args, "reboot", False) and not args.apply:
        raise ResetError("--reboot requires --apply.")
    if args.action in ("restore", "finalize"):
        # Validate every local backup before opening a device connection.
        _, saved, identity, _ = load_backup(args.backup)
        if args.udid is None:
            args.udid = identity.udid
        # The saved transaction already recorded the verified model; use it to
        # confirm identity on a bare ramdisk that reports no sysctl.
        if getattr(args, "model", None) is None:
            args.model = identity.model
    state = args.state_dir.expanduser().absolute()
    args.state_dir = state
    if args.action in ("reset", "prepare") and args.backups is None:
        args.backups = state / "backups"
    from .macos_agents import recovery_agents
    # Hold the Mac device-agent reservation across the whole operation, like the
    # recovery flow. Apple's AMPDevices agents otherwise grab the phone during
    # the ramdisk boot or the SSH session; on Intel Macs they win that race and
    # the device cannot be claimed. No-op off macOS; restores exactly the agents
    # it paused, including on interruption.
    def _announce_usb_wait(attempt):
        # A just-booted ramdisk can take a few seconds to re-register with
        # usbmuxd, especially on Intel Macs over direct USB. Keep polling with
        # on-screen progress instead of failing the first empty query.
        if attempt == 0:
            print("Waiting for the phone to appear on USB "
                  "(it can take a few seconds after boot)...", flush=True)
        if os.environ.get("BL_IOS_UI_PROTOCOL") == "1":
            from .desktop_protocol import progress
            progress("Waiting for the phone",
                     "Waiting for the phone to appear on USB after boot…", None)

    with host_lock(state), recovery_agents():
        if args.action == "prepare":
            launch_backend(args)
        # Only "prepare" just booted a ramdisk, so only it needs to wait for the
        # phone to re-register with usbmuxd. For the other actions the device is
        # already enumerated, so a missing phone should fail immediately (as it
        # did before 2.3.9) rather than stalling and holding the Mac agents paused.
        # Re-enumeration through a USB-C dock or hub (the only way to reach a
        # 30-pin phone from an Apple Silicon Mac) is slower than a direct port,
        # so allow extra headroom for the just-booted ramdisk to reappear.
        wait_timeout = 30.0 if args.action == "prepare" else 0.0
        selected = usbmux.wait_for_device(args.udid, timeout=wait_timeout, on_wait=_announce_usb_wait)
        if args.ask_password and os.environ.get("BL_IOS_UI_PROTOCOL") == "1":
            from .desktop_protocol import prompt
            prompt("password", "Ramdisk SSH password", "Ramdisk SSH root password: ")
        password = (getpass.getpass("Ramdisk SSH root password: ")
                    if args.ask_password else "alpine")
        with USBDevice(selected, state, password, args.port, args.trust_new_key) as device:
            print(f"USB SSH fingerprint: {device.fingerprint}")
            identity = device.inspect(mount=args.mount, writable=getattr(args, "apply", False),
                                      expected_model=getattr(args, "model", None))
            if getattr(args, "model", None) and args.model != identity.model:
                raise ResetError("Booted device does not match --model; no reset was attempted.")
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
        arguments = list(sys.argv[1:] if argv is None else argv)
        from .updates import dispatch
        dispatched = None if os.environ.get("BL_IOS_DESKTOP") == "1" else dispatch(arguments)
        if dispatched is not None:
            return dispatched
        options = parser().parse_args(arguments)
        if sys.platform == "win32" and (
            options.action in ("prepare", "recover")
            or getattr(options, "wsl", False) or getattr(options, "wsl_distro", None)
        ):
            from .wsl import run_in_wsl
            return run_in_wsl(arguments, options)
        return run(options)
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

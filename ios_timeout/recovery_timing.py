"""Wall-clock button guidance with nonblocking, bounded USB observation."""

import argparse
import math
import os
import re
import signal
import subprocess
import tempfile
import time

from .core import ResetError
from .desktop_protocol import progress
from .upstream import early_dfu_state, early_dfu_summary


BOOTLOADERS = {
    "ibss": ("iBSS", "iBoot-636.66.33"),
    "ibec": ("iBEC", "iBoot-931.71.16"),
}


def verify_bootloader_console(model, console, stage="ibss"):
    """Recovery USB identity alone also matches the phone's installed iBoot."""
    if model != "iPhone1,2" or stage not in BOOTLOADERS:
        raise ResetError("No verified bootloader console profile exists for the selected phone.")
    name, version = BOOTLOADERS[stage]
    console = console.replace("\x00", "")
    banners = set(re.findall(r"\b(iBSS|iBEC|iBoot) for ([a-zA-Z0-9]+),", console))
    versions = set(re.findall(r"\bBUILD_TAG:\s*(iBoot-[0-9.]+)(?=\s|$)", console))
    if banners != {(name, "n82ap")} or versions != {version}:
        # Only public build/board fields are emitted; console logs can contain
        # unique device identifiers and must not be copied into diagnostics.
        observed = ", ".join(sorted(kind + " for " + board for kind, board in banners))
        observed += (" / " + ", ".join(sorted(versions))) if versions else ""
        observed = observed[:160] or "no recognizable bootloader banner"
        raise ResetError(f"Recovery mode was detected, but the uploaded {name} was not verified "
                         "(" + observed + "). No ramdisk was uploaded.")
    return name + " for n82ap / " + version


def verify_ibss_console(model, console):
    return verify_bootloader_console(model, console, "ibss")


class RecoveryProbe:
    """At most one query is in flight; a missing phone cannot stop the clock."""

    def __init__(self, command, clock=time.monotonic, timeout=2.0):
        uses_sudo = os.path.basename(command[0]) == "sudo"
        # Linux's backend authenticates sudo in its PTY before this helper.
        # A new session would discard that tty-scoped ticket. Queries must
        # never request another password through their captured output.
        self.command = ([command[0], "-n", *command[1:], "-q"] if uses_sudo
                        else [*command, "-q"])
        self.owns_group = os.name != "nt" and not uses_sudo
        self.clock = clock
        self.timeout = timeout
        self.process = None
        self.next_query = 0.0

    def poll(self):
        now = self.clock()
        if self.process is None:
            if now >= self.next_query:
                self.process = subprocess.Popen(
                    self.command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, text=True,
                    start_new_session=self.owns_group,
                )
                self.started = now
            return None
        if self.process.poll() is not None:
            output, _ = self.process.communicate()
            good = self.process.returncode == 0
            self.process = None
            self.next_query = now + 0.1
            return output[:16384] if good else ""
        if now - self.started >= self.timeout:
            self.close()
            self.next_query = now + 0.1
            return ""
        return None

    def close(self):
        if self.process is not None:
            process, self.process = self.process, None
            if process.poll() is None:
                try:
                    if self.owns_group:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # A macOS query can exit between poll() and killpg().
                    # Reap it first; if still alive, target our child directly.
                    if process.poll() is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
            try:
                process.communicate(timeout=0.3)
            except subprocess.TimeoutExpired as exc:
                process.stdout.close()
                raise ResetError("USB query could not be stopped; recovery cancelled.") from exc

    def bootloader_console(self):
        self.close()
        # The local /exit instruction only closes irecovery's console; it is
        # intercepted by the CLI instead of being sent to the phone. Use a
        # private working directory for its optional command-history file.
        with tempfile.TemporaryDirectory(prefix="openup-boot-console-") as temporary:
            try:
                result = subprocess.run(
                    [*self.command[:-1], "-s"], input="/exit\n", text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=5, cwd=temporary,
                )
            except subprocess.TimeoutExpired as exc:
                raise ResetError("Recovery console verification timed out. No ramdisk was uploaded.") from exc
        if result.returncode:
            raise ResetError("Recovery console could not be read. No ramdisk was uploaded.")
        return result.stdout[:65536]


def guide(model, probe, *, emit=progress, clock=time.monotonic, sleep=time.sleep,
          phases=None, output=print):
    """Each phase ends at its deadline, independently of query completion."""
    phases = phases or (
        ("Hold TOP and HOME buttons.", 10),
        ("Release TOP, keep holding HOME.", 15),
        ("Hold HOME while waiting for the phone.", 15),
    )
    last_readable = ""
    current = ""
    try:
        for message, duration in phases:
            deadline = clock() + duration
            last_remaining = None
            output("* " + message, flush=True)
            while True:
                now = clock()
                if now >= deadline:
                    break
                remaining = math.ceil(deadline - now)
                if remaining != last_remaining:
                    emit("Prepare the phone for DFU", message, remaining)
                    output(str(remaining), flush=True)
                    last_remaining = remaining
                result = probe.poll()
                if result is not None:
                    current = result
                    if current:
                        last_readable = current
                    if early_dfu_state(model, current) in ("wtf", "patched"):
                        emit("Phone detected", "Release both buttons now.", 0)
                        output("* USB entry mode detected. Release both buttons now.", flush=True)
                        return
                sleep(min(0.025, max(0, deadline - clock())))
        summary = early_dfu_summary(current)
        output("[WARNING] Current USB state: " + summary, flush=True)
        if not current and last_readable:
            output("[WARNING] Last readable USB state: " + early_dfu_summary(last_readable), flush=True)
        raise ResetError(
            "S5L8900 wtf verification timed out. Release both buttons. "
            "Force restart the phone before retrying the on-screen sequence. "
            "No board-specific firmware was uploaded."
        )
    finally:
        probe.close()


def wait_bootloader(model, probe, *, stage="ibss", timeout=30, emit=progress,
                    clock=time.monotonic, sleep=time.sleep, output=print):
    """A successful upload is not evidence that the requested stage is running."""
    name = BOOTLOADERS[stage][0]
    deadline = clock() + timeout
    last_readable = ""
    current = ""
    last_event = None
    try:
        while clock() < deadline:
            result = probe.poll()
            if result is not None:
                current = result
                if current:
                    last_readable = current
                # Validate chip/board identity before accepting any transition.
                try:
                    early_dfu_state(model, current)
                except ResetError as exc:
                    output(f"[WARNING] USB state after {name}: " + early_dfu_summary(current), flush=True)
                    raise ResetError(f"The USB identity after {name} could not be verified for the selected phone. "
                                     "No ramdisk was uploaded.") from exc
                fields = dict(line.split(":", 1) for line in current.splitlines() if ":" in line)
                mode = fields.get("MODE", "").strip()
                if mode == "Recovery":
                    console = probe.bootloader_console()
                    # A recovery endpoint can appear before its console banner.
                    # During iBEC handoff the previous iBSS may still answer.
                    previous = stage == "ibec" and "iBSS for " in console
                    if previous:
                        verify_ibss_console(model, console)
                    elif console.replace("\x00", "").strip():
                        identity = verify_bootloader_console(model, console, stage)
                        output("[Log] Uploaded bootloader and phone identity verified: " + identity, flush=True)
                        following = "Loading the second-stage bootloader next." if stage == "ibss" else "Loading the ramdisk next."
                        emit("Recovery bootloader detected", name + " is running. " + following, 0)
                        return
            remaining = math.ceil(deadline - clock())
            description = early_dfu_summary(current) if current else "USB device is reconnecting"
            event = (description, remaining)
            if event != last_event:
                emit(f"Waiting for {name} to start", description + ". The ramdisk has not started yet.", remaining)
                last_event = event
            sleep(min(0.05, max(0, deadline - clock())))
        output("[WARNING] Current USB state: " + early_dfu_summary(current), flush=True)
        if not current and last_readable:
            output("[WARNING] Last readable USB state: " + early_dfu_summary(last_readable), flush=True)
        state = early_dfu_state(model, current)
        if state in ("stock", "patched"):
            reason = f"The phone is still in DFU; {name} did not start."
        else:
            reason = f"The phone did not reappear with a verified {name} console."
        # This also reproduces on Intel Macs with a direct connection. USB
        # enumeration and a successful transfer cannot identify the cause.
        raise ResetError(reason + " The ramdisk was not uploaded. "
                         "The cause of the boot handoff failure is not yet known. "
                         "Save the session log. Before another attempt, force restart the phone "
                         "and follow the on-screen button steps for a fresh session.")
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_mutually_exclusive_group()
    stages.add_argument("--after-ibss", action="store_true")
    stages.add_argument("--after-ibec", action="store_true")
    parser.add_argument("model")
    parser.add_argument("query", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.query[1:] if args.query[:1] == ["--"] else args.query
    if not command:
        parser.error("A recovery query executable is required.")
    try:
        if args.after_ibss or args.after_ibec:
            wait_bootloader(args.model, RecoveryProbe(command), stage="ibec" if args.after_ibec else "ibss")
        else:
            guide(args.model, RecoveryProbe(command))
        return 0
    except (ResetError, OSError) as exc:
        print("[Error] " + str(exc), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

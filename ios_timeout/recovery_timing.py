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
# 2G's pinned 3.1.3 / 7E18 iBSS has been inspected directly: m68ap,
# iBoot-636.66.33. That route boots its ramdisk directly from iBSS, without iBEC.
BOOT_BOARDS = {"iPhone1,1": ("m68ap", 0), "iPhone1,2": ("n82ap", 4)}
# A newly launched bootloader can take time to present its recovery console.
# Keep reads and the overall stage bounded, while polling the child process so
# neither USB initialization nor console startup freezes the countdown.
CONSOLE_READ_TIMEOUT = 30
STAGE_WAIT_TIMEOUT = 90
CONSOLE_OUTPUT_LIMIT = 65536


class ConsoleReadError(ResetError):
    """An incomplete console observation may be retried before the deadline."""


def verify_bootloader_console(model, console, stage="ibss"):
    """Recovery USB identity alone also matches the phone's installed iBoot."""
    if model not in BOOT_BOARDS or stage not in BOOTLOADERS or (model == "iPhone1,1" and stage != "ibss"):
        raise ResetError("No verified bootloader console profile exists for the selected phone.")
    board = BOOT_BOARDS[model][0]
    name, version = BOOTLOADERS[stage]
    console = console.replace("\x00", "")
    banners = set(re.findall(r"\b(iBSS|iBEC|iBoot) for ([a-zA-Z0-9]+),", console))
    versions = set(re.findall(r"\bBUILD_TAG:\s*(iBoot-[0-9.]+)(?=\s|$)", console))
    if banners != {(name, board)} or versions != {version}:
        # Only public build/board fields are emitted; console logs can contain
        # unique device identifiers and must not be copied into diagnostics.
        observed = ", ".join(sorted(kind + " for " + board for kind, board in banners))
        observed += (" / " + ", ".join(sorted(versions))) if versions else ""
        observed = observed[:160] or "no recognizable bootloader banner"
        raise ResetError(f"Recovery mode was detected, but the uploaded {name} was not verified "
                         "(" + observed + "). No ramdisk was uploaded.")
    return name + " for " + board + " / " + version


def verify_ibss_console(model, console):
    return verify_bootloader_console(model, console, "ibss")


class RecoveryProbe:
    """At most one query is in flight; a missing phone cannot stop the clock."""

    def __init__(self, command, clock=time.monotonic, timeout=2.0):
        uses_sudo = os.path.basename(command[0]) in ("sudo", "sudo.ws")
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
        self.console_directory = None
        self.console_output = None

    def poll(self):
        if self.console_directory is not None:
            raise ResetError("A recovery console read is already in progress.")
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
        try:
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
                    if process.stdout is not None:
                        process.stdout.close()
                    raise ResetError("USB query could not be stopped; recovery cancelled.") from exc
        finally:
            if self.console_output is not None:
                self.console_output.close()
                self.console_output = None
            if self.console_directory is not None:
                self.console_directory.cleanup()
                self.console_directory = None

    def bootloader_console(self):
        """Poll one bounded console read; None means it is still in flight.

        Only a completed, zero-status read is evidence. Partial timeout output
        may contain stale banners and is never accepted or logged.
        """
        if self.console_directory is None:
            self.close()
            # /exit is local to irecovery, not a command sent to the phone.
            # Keep its optional history and captured output in private temporary
            # storage. File-backed stdout cannot fill a pipe and deadlock a
            # verbose bootloader while the UI continues polling.
            self.console_directory = tempfile.TemporaryDirectory(prefix="openup-boot-console-")
            try:
                self.console_output = tempfile.TemporaryFile()
                self.process = subprocess.Popen(
                    [*self.command[:-1], "-s"], stdin=subprocess.PIPE,
                    stdout=self.console_output, stderr=subprocess.STDOUT,
                    cwd=self.console_directory.name, start_new_session=self.owns_group,
                )
                self.started = self.clock()
                try:
                    with self.process.stdin as pipe:
                        pipe.write(b"/exit\n")
                except BrokenPipeError:
                    pass  # An early exit is evaluated below by its return code.
                finally:
                    self.process.stdin = None
            except BaseException:
                self.close()
                raise
            return None
        if os.fstat(self.console_output.fileno()).st_size > CONSOLE_OUTPUT_LIMIT:
            self.close()
            raise ConsoleReadError("Recovery console output exceeded its limit. No ramdisk was uploaded.")
        if self.process.poll() is not None:
            good = self.process.returncode == 0
            self.console_output.seek(0)
            output = self.console_output.read(CONSOLE_OUTPUT_LIMIT + 1)
            self.close()
            if not good or len(output) > CONSOLE_OUTPUT_LIMIT:
                raise ConsoleReadError("Recovery console could not be read. No ramdisk was uploaded.")
            return output.decode("utf-8", errors="replace")
        if self.clock() - self.started >= CONSOLE_READ_TIMEOUT:
            self.close()
            raise ConsoleReadError("Recovery console verification timed out. No ramdisk was uploaded.")
        return None


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
                        if model == "iPhone1,1":
                            from .physical_usb import require_single_mobile_device
                            require_single_mobile_device(deadline=deadline)
                            if clock() >= deadline:
                                break
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


def _boot_query_state(model, snapshot):
    """Retry incomplete observations, but stop on a concrete different phone."""
    fields = {}
    for key, value in re.findall(r"^([A-Z]+):[ \t]*(.*?)[ \t\r]*$", snapshot, re.MULTILINE):
        if key in ("MODE", "CPID", "BDID") and key in fields:
            raise ResetError("Conflicting USB identity fields; recovery cancelled. No ramdisk was uploaded.")
        fields[key] = value
    if model not in BOOT_BOARDS:
        raise ResetError("No verified bootloader console profile exists for the selected phone.")
    for key, expected in (("CPID", 0x8900), ("BDID", BOOT_BOARDS[model][1])):
        if key == "BDID" and fields.get("MODE") == "WTF":
            continue  # The shared WTF endpoint cannot identify the real board.
        try:
            observed = int(fields[key], 16)
        except (KeyError, ValueError):
            continue
        if observed != expected:
            raise ResetError("The USB chip/board identity does not match the selected iPhone. "
                             "Recovery cancelled. No ramdisk was uploaded.")
    try:
        state = early_dfu_state(model, snapshot)
    except ResetError:
        return None  # A missing or malformed chip/board field cannot authorize a read.
    return "recovery" if fields.get("MODE") == "Recovery" else state


def wait_bootloader(model, probe, *, stage="ibss", timeout=STAGE_WAIT_TIMEOUT, emit=progress,
                    clock=time.monotonic, sleep=time.sleep, output=print):
    """A successful upload is not evidence that the requested stage is running."""
    if model not in BOOT_BOARDS or stage not in BOOTLOADERS or (model == "iPhone1,1" and stage != "ibss"):
        raise ResetError("No verified bootloader console profile exists for the selected phone.")
    name = BOOTLOADERS[stage][0]
    deadline = clock() + timeout
    last_readable = ""
    current = ""
    last_event = None
    reading_console = False
    last_console_error = ""
    try:
        while clock() < deadline:
            if not reading_console:
                result = probe.poll()
                if result is not None:
                    current = result
                    if current:
                        last_readable = current
                    reading_console = _boot_query_state(model, current) == "recovery"
                    if reading_console and model == "iPhone1,1":
                        from .physical_usb import require_single_mobile_device
                        require_single_mobile_device(deadline=deadline)
                        if clock() >= deadline:
                            break
            if reading_console:
                try:
                    console = probe.bootloader_console()
                except ConsoleReadError as exc:
                    console = ""
                    last_console_error = str(exc)
                if console is not None:
                    reading_console = False
                    # A recognized different board is not a reconnect delay.
                    # Installed iBoot, the previous iBSS, incomplete text, and
                    # mixed banners remain unverified and may be read again.
                    boards = re.findall(r"\b(?:iBSS|iBEC|iBoot) for ([a-zA-Z0-9]+),", console)
                    if any(board != BOOT_BOARDS[model][0] for board in boards):
                        raise ResetError("Recovery console board does not match the selected iPhone. "
                                         "No ramdisk was uploaded.")
                    try:
                        identity = verify_bootloader_console(model, console, stage)
                    except ResetError as exc:
                        if console:
                            last_console_error = str(exc)
                    else:
                        if model == "iPhone1,1":
                            from .physical_usb import require_single_mobile_device
                            require_single_mobile_device(deadline=deadline)
                        # No late result can authorize the next upload after
                        # the overall deadline, even if its banner is correct.
                        if clock() >= deadline:
                            break
                        output("[Log] Uploaded bootloader and phone identity verified: " + identity, flush=True)
                        following = ("Loading the second-stage bootloader next."
                                     if stage == "ibss" and model == "iPhone1,2" else "Loading the ramdisk next.")
                        emit("Recovery bootloader detected", name + " is running. " + following, 0)
                        return
            remaining = max(0, math.ceil(deadline - clock()))
            description = ("Reading the recovery console" if reading_console else
                           early_dfu_summary(current) if current else "USB device is reconnecting")
            event = (description, remaining)
            if event != last_event:
                emit(f"Waiting for {name} to start", description + ". The ramdisk has not started yet.", remaining)
                last_event = event
            sleep(min(0.05, max(0, deadline - clock())))
        output("[WARNING] Current USB state: " + early_dfu_summary(current), flush=True)
        if not current and last_readable:
            output("[WARNING] Last readable USB state: " + early_dfu_summary(last_readable), flush=True)
        if last_console_error:
            output("[WARNING] Last console check: " + last_console_error, flush=True)
        state = _boot_query_state(model, current or last_readable)
        if state in ("stock", "patched"):
            reason = f"The phone is still in DFU; {name} did not start."
        else:
            reason = f"The phone did not reappear with a verified {name} console."
        # USB enumeration and a successful transfer cannot identify the cause.
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

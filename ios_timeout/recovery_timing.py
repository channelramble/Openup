"""Wall-clock button guidance with nonblocking, bounded USB observation."""

import argparse
import math
import os
import signal
import subprocess
import time

from .core import ResetError
from .desktop_protocol import progress
from .upstream import early_dfu_state, early_dfu_summary


class RecoveryProbe:
    """At most one query is in flight; a missing phone cannot stop the clock."""

    def __init__(self, command, clock=time.monotonic, timeout=2.0):
        self.command = [*command, "-q"]
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
                    start_new_session=os.name != "nt",
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
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=0.3)
            except subprocess.TimeoutExpired as exc:
                process.stdout.close()
                raise ResetError("USB query could not be stopped; recovery cancelled.") from exc


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("query", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.query[1:] if args.query[:1] == ["--"] else args.query
    if not command:
        parser.error("A recovery query executable is required.")
    try:
        guide(args.model, RecoveryProbe(command))
        return 0
    except (ResetError, OSError) as exc:
        print("[Error] " + str(exc), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

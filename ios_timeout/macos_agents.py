"""Suspend Apple's per-user device handlers for one managed recovery session.

A detached guardian owns the suspension. Closing its lease (including parent
death) restores the same process identities, never every process with a name.
"""

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from contextlib import contextmanager


AGENTS = frozenset((
    "/System/Library/PrivateFrameworks/AMPDevices.framework/Versions/A/Support/AMPDevicesAgent",
    "/System/Library/PrivateFrameworks/AMPDevices.framework/Versions/A/Support/AMPDeviceDiscoveryAgent",
    "/System/Library/PrivateFrameworks/MobileDevice.framework/Resources/"
    "MobileDeviceUpdater.app/Contents/MacOS/MobileDeviceUpdater",
))


def snapshot():
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,uid=,stat=,lstart=,comm="],
        capture_output=True, text=True, check=True, timeout=2,
        env={**os.environ, "LC_ALL": "C"},
    )
    return parse_snapshot(result.stdout, os.getuid())


def parse_snapshot(text, uid):
    found = {}
    for line in text.splitlines():
        parts = line.split(None, 8)
        if len(parts) != 9:
            continue
        pid, owner, status, *rest = parts
        path = rest[-1]
        if not pid.isdigit() or not owner.isdigit() or int(owner) != uid or path not in AGENTS:
            continue
        identity = (int(pid), int(owner), " ".join(rest[:-1]), path)
        found[int(pid)] = (identity, status)
    return found


class AgentLease:
    def __init__(self, journal, read=snapshot, send=os.kill):
        self.journal = Path(journal)
        self.read = read
        self.send = send
        self.owned = {}

    def save(self):
        temporary = self.journal.with_suffix(".tmp")
        temporary.write_text(json.dumps(list(self.owned.values())), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.journal)

    def pause(self):
        for pid, (identity, status) in self.read().items():
            if self.owned.get(pid) == identity or "T" in status:
                continue
            # Persist before STOP so even a guardian crash can be recovered.
            self.owned[pid] = identity
            self.save()
            try:
                self.send(pid, signal.SIGSTOP)
            except ProcessLookupError:
                self.owned.pop(pid)
                self.save()
            except BaseException:
                self.owned.pop(pid)
                self.save()
                raise

    def restore(self):
        current = self.read()
        for pid, identity in list(self.owned.items()):
            if pid in current and current[pid][0] == identity:
                try:
                    self.send(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            self.owned.pop(pid)
            self.save()
        self.journal.unlink(missing_ok=True)

    def recover(self):
        if self.journal.exists():
            for item in json.loads(self.journal.read_text(encoding="utf-8")):
                if (isinstance(item, list) and len(item) == 4 and isinstance(item[0], int)
                        and item[1] == os.getuid() and item[3] in AGENTS):
                    self.owned[item[0]] = tuple(item)
            self.restore()


def guardian(lease_fd, ready_fd, directory):
    from ios_timeout.host import host_lock
    stopped = False

    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with host_lock(directory):
        agents = AgentLease(Path(directory) / "paused.json")
        try:
            agents.recover()
            agents.pause()
            print("[Log] Mac device auto-handling paused for this recovery session.", flush=True)
            os.write(ready_fd, b"1")
            os.close(ready_fd)
            ready_fd = None
            while not stopped:
                readable, _, _ = select.select([lease_fd], [], [], 0.3)
                if readable and not os.read(lease_fd, 1):
                    break
                agents.pause()  # Also cover agents started after the session.
        finally:
            agents.restore()
            if ready_fd is not None:
                os.close(ready_fd)
            os.close(lease_fd)
            print("[Log] Mac device auto-handling restored.", flush=True)


@contextmanager
def recovery_agents():
    if sys.platform != "darwin":
        yield
        return
    from .core import ResetError
    from .host import host_lock, state_directory
    directory = state_directory() / "device-agent-guard"
    lease_read, lease_write = os.pipe()
    ready_read, ready_write = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
             str(lease_read), str(ready_write), str(directory)],
            pass_fds=(lease_read, ready_write), start_new_session=True,
            stdin=subprocess.DEVNULL,
        )
        os.close(lease_read)
        lease_read = None
        os.close(ready_write)
        ready_write = None
        ready, _, _ = select.select([ready_read], [], [], 10)
        if not ready or os.read(ready_read, 1) != b"1":
            raise ResetError("Could not reserve Mac device handling; recovery was not started.")
        yield
    finally:
        for fd in (lease_read, lease_write, ready_read, ready_write):
            if fd is not None:
                os.close(fd)
        if process is not None:
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            if process.returncode:
                # A killed guardian still leaves a journal of exactly its PIDs.
                with host_lock(directory):
                    AgentLease(directory / "paused.json").recover()


if __name__ == "__main__":
    # The guardian runs directly with isolated Python, including in signed apps.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(guardian(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]))

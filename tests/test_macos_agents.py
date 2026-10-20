import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from ios_timeout import macos_agents as agents


@unittest.skipIf(os.name == "nt", "POSIX process signals")
class AgentLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent lease ")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "paused.json"
        self.identity = (123, os.getuid(), "Tue Oct 20 10:00:00 2026", sorted(agents.AGENTS)[0])
        self.processes = {123: (self.identity, "S")}
        self.signals = []
        self.lease = agents.AgentLease(self.path, read=lambda: self.processes,
                                       send=lambda *args: self.signals.append(args))

    def test_only_current_users_exact_system_agent_paths_match(self):
        uid = os.getuid()
        path = sorted(agents.AGENTS)[0]
        text = (
            f"123 {uid} S Tue Oct 20 10:00:00 2026 {path}\n"
            f"124 {uid + 1} S Tue Oct 20 10:00:00 2026 {path}\n"
            f"125 {uid} S Tue Oct 20 10:00:00 2026 /tmp/AMPDevicesAgent\n"
            f"126 {uid} S Tue Oct 20 10:00:00 2026 {path} --forged\n"
        )
        self.assertEqual(agents.parse_snapshot(text, uid), {123: (self.identity, "S")})

    def test_pause_is_journaled_before_signal_and_restores_exact_identity(self):
        def send(pid, sig):
            if sig == signal.SIGSTOP:
                self.assertEqual(json.loads(self.path.read_text())[0][0], pid)
            self.signals.append((pid, sig))
        self.lease.send = send
        self.lease.pause()
        self.lease.pause()
        self.lease.restore()
        self.assertEqual(self.signals, [(123, signal.SIGSTOP), (123, signal.SIGCONT)])
        self.assertFalse(self.path.exists())

    def test_preexisting_stopped_agent_is_never_resumed(self):
        self.processes[123] = (self.identity, "T")
        self.lease.pause()
        self.lease.restore()
        self.assertEqual(self.signals, [])

    def test_recycled_pid_is_not_resumed(self):
        self.lease.pause()
        changed = (*self.identity[:2], "Tue Oct 20 11:00:00 2026", self.identity[3])
        self.processes[123] = (changed, "S")
        self.lease.restore()
        self.assertEqual(self.signals, [(123, signal.SIGSTOP)])

    def test_new_agents_are_acquired_and_failed_pause_does_not_claim_them(self):
        self.lease.pause()
        second = (124, *self.identity[1:])
        self.processes[124] = (second, "S")
        self.lease.pause()
        self.assertEqual(self.signals, [(123, signal.SIGSTOP), (124, signal.SIGSTOP)])
        self.lease.restore()
        self.lease.send = lambda *a: (_ for _ in ()).throw(PermissionError("fixture"))
        with self.assertRaises(PermissionError):
            self.lease.pause()
        self.assertEqual(self.lease.owned, {})

    def test_crashed_guardian_journal_can_be_recovered(self):
        self.lease.pause()
        replacement = agents.AgentLease(self.path, read=lambda: self.processes,
                                        send=lambda *a: self.signals.append(a))
        replacement.recover()
        self.assertEqual(self.signals[-1], (123, signal.SIGCONT))
        self.assertFalse(self.path.exists())

    def test_guardian_resumes_a_real_fixture_when_parent_lease_closes(self):
        # A real subprocess receives STOP/CONT. Apple agents are never touched.
        fixture = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: fixture.poll() is None and fixture.kill())
        read_fd, write_fd = os.pipe()
        ready_fd, ack_fd = os.pipe()
        root = str(Path(agents.__file__).resolve().parent.parent)
        code = """
import os, sys
sys.path.insert(0, sys.argv[1])
from ios_timeout import macos_agents as m
pid, rfd, afd = map(int, sys.argv[2:5])
identity = (pid, os.getuid(), 'fixture-start', sorted(m.AGENTS)[0])
m.snapshot = lambda: {pid: (identity, 'S')}
original = m.AgentLease
m.AgentLease = lambda journal: original(journal, read=m.snapshot)
m.guardian(rfd, afd, sys.argv[5])
"""
        guard = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", code, root, str(fixture.pid),
             str(read_fd), str(ack_fd), self.temp.name],
            pass_fds=(read_fd, ack_fd), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        os.close(read_fd)
        os.close(ack_fd)
        try:
            import select
            self.assertTrue(select.select([ready_fd], [], [], 5)[0])
            self.assertEqual(os.read(ready_fd, 1), b"1")
            self.assertIn("T", subprocess.check_output(
                ["ps", "-o", "stat=", "-p", str(fixture.pid)], text=True))
            os.close(write_fd)
            write_fd = None
            _, errors = guard.communicate(timeout=5)
            self.assertEqual(guard.returncode, 0, errors)
            self.assertFalse(self.path.exists())
            self.assertNotIn("T", subprocess.check_output(
                ["ps", "-o", "stat=", "-p", str(fixture.pid)], text=True))
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(ready_fd)
            if guard.poll() is None:
                guard.kill()
                guard.wait()
            os.kill(fixture.pid, signal.SIGCONT)
            fixture.terminate()
            fixture.wait(timeout=3)

    def test_other_platforms_do_not_launch_guardian(self):
        with patch.object(agents.sys, "platform", "linux"), patch.object(agents.subprocess, "Popen") as launch:
            with agents.recovery_agents():
                pass
            launch.assert_not_called()

    def test_backend_failure_exits_agent_lease(self):
        from ios_timeout import upstream
        from ios_timeout.core import ResetError
        backend = Path(self.temp.name) / "backend"
        backend.mkdir()
        (backend / "Bruteforce.sh").write_bytes(b"fixture")
        calls = []

        @contextmanager
        def lease():
            calls.append("enter")
            try:
                yield
            finally:
                calls.append("exit")

        args = SimpleNamespace(apply=True, install_only=False, backend_dir=backend,
                               state_dir=Path(self.temp.name), model="iPhone1,2",
                               armv6_tools=None)
        with patch.object(upstream, "checkout_backend", return_value=backend), \
             patch.object(upstream, "adapt_script", return_value=b"#!/bin/bash\nexit 7\n"), \
             patch.object(agents, "recovery_agents", lease), \
             self.assertRaisesRegex(ResetError, "status 7"):
            upstream.run_backend(args)
        self.assertEqual(calls, ["enter", "exit"])


if __name__ == "__main__":
    unittest.main()

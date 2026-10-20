import os
import json
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ios_timeout.core import ResetError
from ios_timeout.recovery_timing import RecoveryProbe, guide, wait_bootloader


WTF = "MODE: WTF\nCPID: 8900\nBDID: 00\n"
STOCK = "MODE: DFU\nCPID: 8900\nBDID: 04\nSRTG: iBoot-596.24\n"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class Probe:
    def __init__(self, clock, ready_at=None, result=WTF):
        self.clock = clock
        self.ready_at = ready_at
        self.result = result
        self.closed = False

    def poll(self):
        if self.ready_at is not None and self.clock() >= self.ready_at:
            return self.result
        return None  # A slow/in-flight query, not a synchronous sleep.

    def close(self):
        self.closed = True


class RecoveryTimingTests(unittest.TestCase):
    def test_ibss_upload_requires_matching_recovery_mode(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0.35, result=STOCK.replace("DFU", "Recovery"))
        events = []
        wait_bootloader("iPhone1,2", probe, timeout=1, clock=clock,
                        sleep=clock.sleep, emit=lambda *a: events.append(a),
                        output=lambda *a, **k: None)
        self.assertLess(clock(), 0.41)
        self.assertEqual(events[-1][0], "Recovery bootloader detected")
        self.assertTrue(probe.closed)

    def test_still_dfu_reports_actual_state_without_claiming_ramdisk_boot(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK + "ECID: private-id\n")
        events, lines = [], []
        with self.assertRaisesRegex(ResetError, "still in DFU.*ramdisk was not uploaded"):
            wait_bootloader("iPhone1,2", probe, timeout=1, clock=clock,
                            sleep=clock.sleep, emit=lambda *a: events.append(a),
                            output=lambda value, **k: lines.append(value))
        self.assertAlmostEqual(clock(), 1)
        self.assertTrue(probe.closed)
        self.assertTrue(all(event[0] == "Waiting for iBSS to start" for event in events))
        self.assertIn("MODE=DFU", "\n".join(lines))
        self.assertNotIn("private-id", str(events) + str(lines))

    def test_recovery_on_wrong_board_or_without_identity_is_rejected(self):
        for snapshot in ("MODE: Recovery\n", STOCK.replace("DFU", "Recovery").replace("04", "00")):
            clock = Clock()
            probe = Probe(clock, ready_at=0, result=snapshot)
            with self.subTest(snapshot=snapshot), self.assertRaises(ResetError):
                wait_bootloader("iPhone1,2", probe, clock=clock, sleep=clock.sleep,
                                emit=lambda *a: None, output=lambda *a, **k: None)
            self.assertTrue(probe.closed)

    def test_completed_query_is_reaped_without_signalling_its_old_process_group(self):
        probe = RecoveryProbe([sys.executable])
        process = probe.process = Mock()
        process.poll.return_value = 0
        with patch("ios_timeout.recovery_timing.os.killpg", create=True) as kill:
            probe.close()
        kill.assert_not_called()
        process.kill.assert_not_called()
        process.communicate.assert_called_once_with(timeout=0.3)
        self.assertIsNone(probe.process)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_query_exit_race_does_not_turn_success_into_permission_error(self):
        probe = RecoveryProbe([sys.executable])
        process = probe.process = Mock()
        process.poll.side_effect = [None, 0]
        with patch("ios_timeout.recovery_timing.os.killpg", side_effect=PermissionError):
            probe.close()
        process.kill.assert_not_called()
        process.communicate.assert_called_once_with(timeout=0.3)

    def test_missing_usb_does_not_stretch_button_phase_deadlines(self):
        clock = Clock()
        probe = Probe(clock)
        events = []
        with self.assertRaisesRegex(ResetError, "verification timed out"):
            guide("iPhone1,2", probe, clock=clock, sleep=clock.sleep,
                  emit=lambda *args: events.append((clock(), args)),
                  output=lambda *args, **kwargs: None)
        self.assertAlmostEqual(clock(), 40, places=6)
        self.assertTrue(probe.closed)
        self.assertEqual(events[0][1][-1], 10)
        release = next(event for event in events if event[1][1].startswith("Release TOP"))
        self.assertAlmostEqual(release[0], 10, places=6)
        self.assertEqual(release[1][-1], 15)
        self.assertEqual([e[1][-1] for e in events[:10]], list(range(10, 0, -1)))

    def test_detection_stops_guidance_immediately_and_closes_probe(self):
        clock = Clock()
        probe = Probe(clock, ready_at=12.4)
        events = []
        guide("iPhone1,2", probe, clock=clock, sleep=clock.sleep,
              emit=lambda *args: events.append(args), output=lambda *a, **k: None)
        self.assertLess(clock(), 12.43)
        self.assertEqual(events[-1], ("Phone detected", "Release both buttons now.", 0))
        self.assertTrue(probe.closed)

    def test_wrong_board_stops_and_stock_dfu_never_counts_as_success(self):
        for snapshot in (STOCK, STOCK.replace("04", "00")):
            clock = Clock()
            probe = Probe(clock, ready_at=0, result=snapshot)
            with self.assertRaises(ResetError):
                guide("iPhone1,2", probe, clock=clock, sleep=clock.sleep,
                      phases=(("Hold HOME.", 0.1),),
                      emit=lambda *a: None, output=lambda *a, **k: None)
            self.assertTrue(probe.closed)

    def test_timeout_diagnostics_do_not_expose_unique_device_identifiers(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK + "ECID: private-id\n")
        lines = []
        with self.assertRaises(ResetError):
            guide("iPhone1,2", probe, clock=clock, sleep=clock.sleep,
                  phases=(("Hold HOME.", 0.1),),
                  emit=lambda *a: None, output=lambda value, **k: lines.append(value))
        self.assertIn("SRTG=iBoot-596.24", "\n".join(lines))
        self.assertNotIn("private-id", "\n".join(lines))

    def test_real_blocked_query_does_not_block_clock_and_is_reaped(self):
        probe = RecoveryProbe([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
        started = time.monotonic()
        with self.assertRaises(ResetError):
            guide("iPhone1,2", probe, phases=(("Hold HOME.", 0.2),),
                  emit=lambda *a: None, output=lambda *a, **k: None)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNone(probe.process)

    def test_query_failure_cannot_authorize_entry(self):
        code = "import sys; print(" + repr(WTF) + "); sys.exit(9)"
        probe = RecoveryProbe([sys.executable, "-B", "-c", code])
        try:
            deadline = time.monotonic() + 3
            result = None
            while result is None and time.monotonic() < deadline:
                result = probe.poll()
                time.sleep(0.01)
            self.assertEqual(result, "")
        finally:
            probe.close()

    @unittest.skipIf(os.name == "nt", "POSIX terminal session")
    def test_sudo_query_keeps_backend_session_and_cannot_prompt_for_password(self):
        with tempfile.TemporaryDirectory(prefix="query with spaces ") as folder:
            sudo = Path(folder) / "sudo"
            code = "import os,sys,json;print(json.dumps([os.getsid(0),sys.argv[1:]]))"
            sudo.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) +
                            " -B -c " + shlex.quote(code) + ' "$@"\n')
            sudo.chmod(0o700)
            probe = RecoveryProbe([str(sudo), "/tool with spaces/irecovery"])
            try:
                result = None
                deadline = time.monotonic() + 3
                while result is None and time.monotonic() < deadline:
                    result = probe.poll()
                    time.sleep(0.01)
                session, arguments = json.loads(result)
                self.assertEqual(session, os.getsid(0))
                self.assertEqual(arguments, ["-n", "/tool with spaces/irecovery", "-q"])
            finally:
                probe.close()


if __name__ == "__main__":
    unittest.main()

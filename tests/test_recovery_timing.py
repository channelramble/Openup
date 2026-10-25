import os
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ios_timeout.core import ResetError
from ios_timeout.recovery_timing import (ConsoleReadError, RecoveryProbe, guide, wait_bootloader,
                                         verify_ibss_console, verify_bootloader_console)


WTF = "MODE: WTF\nCPID: 8900\nBDID: 00\n"
STOCK = "MODE: DFU\nCPID: 8900\nBDID: 04\nSRTG: iBoot-596.24\n"
IBSS_CONSOLE = "\x00:: iBSS for n82ap, Copyright 2009, Apple Inc.\n\x00:: BUILD_TAG: iBoot-636.66.33\n"
IBEC_CONSOLE = IBSS_CONSOLE.replace("iBSS", "iBEC").replace("636.66.33", "931.71.16")


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

    def bootloader_console(self):
        return IBSS_CONSOLE


class RecoveryTimingTests(unittest.TestCase):
    def test_ibec_requires_exact_identity_even_with_same_stock_build(self):
        self.assertIn("iBEC", verify_bootloader_console("iPhone1,2", IBEC_CONSOLE, "ibec"))
        for console in (IBEC_CONSOLE.replace("iBEC for", "iBoot for"), IBSS_CONSOLE,
                        IBEC_CONSOLE.replace("n82ap", "m68ap"), IBEC_CONSOLE + IBSS_CONSOLE):
            with self.subTest(console=console), self.assertRaises(ResetError):
                verify_bootloader_console("iPhone1,2", console, "ibec")

    def test_ibec_handoff_waits_for_console_after_previous_stage(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        probe.bootloader_console = Mock(side_effect=[IBSS_CONSOLE, "", IBEC_CONSOLE])
        events = []
        wait_bootloader("iPhone1,2", probe, stage="ibec", timeout=1, clock=clock,
                        sleep=clock.sleep, emit=lambda *a: events.append(a), output=lambda *a, **k: None)
        self.assertEqual(probe.bootloader_console.call_count, 3)
        self.assertIn("iBEC is running", events[-1][1])

    def test_empty_console_never_confirms_bootloader(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        probe.bootloader_console = lambda: ""
        with self.assertRaisesRegex(ResetError, "verified iBEC console"):
            wait_bootloader("iPhone1,2", probe, stage="ibec", timeout=1, clock=clock,
                            sleep=clock.sleep, emit=lambda *a: None, output=lambda *a, **k: None)

    def test_console_requires_the_uploaded_ibss_not_installed_iboot(self):
        self.assertIn("636.66.33", verify_ibss_console("iPhone1,2", IBSS_CONSOLE))
        installed = IBSS_CONSOLE.replace("iBSS for", "iBoot for").replace("636.66.33", "931.71.16")
        for console in (installed, "", IBSS_CONSOLE.replace("n82ap", "m68ap"),
                        IBSS_CONSOLE.replace("636.66.33", "931.71.16"),
                        IBSS_CONSOLE + installed):
            with self.subTest(console=console), self.assertRaises(ResetError):
                verify_ibss_console("iPhone1,2", console)

    def test_installed_iboot_in_recovery_never_reports_ibss_running(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        probe.bootloader_console = lambda: IBSS_CONSOLE.replace("iBSS for", "iBoot for") + "ECID: private-id\n"
        events = []
        with self.assertRaisesRegex(ResetError, "did not reappear with a verified iBSS console") as failure:
            wait_bootloader("iPhone1,2", probe, timeout=1, clock=clock, sleep=clock.sleep,
                            emit=lambda *args: events.append(args), output=lambda *a, **k: None)
        self.assertTrue(probe.closed)
        self.assertNotIn("private-id", str(failure.exception))
        self.assertFalse(any(event[0] == "Recovery bootloader detected" for event in events))

    def test_flaky_or_slow_console_reads_retry_until_a_clean_match(self):
        # A slow or incomplete read is not proof that the bootloader failed.
        # Only a later complete, exact match may authorize the next upload.
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        reads = [
            ConsoleReadError("Recovery console verification timed out. No ramdisk was uploaded."),
            "garbage \x00 partial read",
            IBEC_CONSOLE.replace("iBEC for", "iBoot for"),  # looks like installed iBoot
            IBEC_CONSOLE,                                    # finally a clean iBEC read
        ]

        def flaky():
            item = reads.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        probe.bootloader_console = flaky
        events = []
        wait_bootloader("iPhone1,2", probe, stage="ibec", timeout=5, clock=clock,
                        sleep=clock.sleep, emit=lambda *a: events.append(a), output=lambda *a, **k: None)
        self.assertEqual(events[-1][0], "Recovery bootloader detected")
        self.assertEqual(reads, [])  # all four reads consumed; only the clean iBEC won

    def test_console_reader_uses_local_exit_and_private_history_directory(self):
        code = "import os,sys,json; print(json.dumps([os.getcwd(),sys.argv[-1],sys.stdin.readline()]))"
        probe = RecoveryProbe([sys.executable, "-B", "-c", code])
        try:
            result = None
            deadline = time.monotonic() + 3
            while result is None and time.monotonic() < deadline:
                result = probe.bootloader_console()
                time.sleep(0.01)
            folder, argument, instruction = json.loads(result)
            self.assertEqual(argument, "-s")
            self.assertEqual(instruction, "/exit\n")
            self.assertIn("openup-boot-console-", folder)
            self.assertFalse(Path(folder).exists())
        finally:
            probe.close()

    def test_slow_console_does_not_freeze_countdown_or_exceed_stage_deadline(self):
        code = ("import sys,time; "
                "print(" + repr(STOCK.replace("DFU", "Recovery")) + ") if sys.argv[-1]=='-q' "
                "else (print(" + repr(IBEC_CONSOLE) + ",flush=True),time.sleep(30))")
        probe = RecoveryProbe([sys.executable, "-B", "-c", code])
        events = []
        started = time.monotonic()
        with self.assertRaisesRegex(ResetError, "verified iBEC console"):
            wait_bootloader("iPhone1,2", probe, stage="ibec", timeout=1.2,
                            emit=lambda *a: events.append(a), output=lambda *a, **k: None)
        self.assertLess(time.monotonic() - started, 2.0)
        console_events = [e for e in events if e[1].startswith("Reading the recovery console")]
        self.assertTrue(console_events)
        self.assertFalse(any(e[0] == "Recovery bootloader detected" for e in events))
        self.assertIsNone(probe.process)
        self.assertIsNone(probe.console_directory)
        self.assertIsNone(probe.console_output)

    def test_console_in_flight_keeps_ticks_and_never_starts_a_second_query(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        probe.poll = Mock(wraps=probe.poll)
        probe.bootloader_console = Mock(return_value=None)
        events = []
        with self.assertRaisesRegex(ResetError, "verified iBEC console"):
            wait_bootloader("iPhone1,2", probe, stage="ibec", timeout=2.2, clock=clock,
                            sleep=clock.sleep, emit=lambda *a: events.append(a), output=lambda *a, **k: None)
        self.assertEqual([e[-1] for e in events], [3, 2, 1])
        self.assertEqual(probe.poll.call_count, 1)
        self.assertAlmostEqual(clock(), 2.2)
        self.assertTrue(probe.closed)

    def test_cancellation_reaps_pending_console_and_removes_private_files(self):
        code = ("import sys,time; "
                "print(" + repr(STOCK.replace("DFU", "Recovery")) + ") if sys.argv[-1]=='-q' "
                "else time.sleep(30)")
        probe = RecoveryProbe([sys.executable, "-B", "-c", code])
        running = []
        def interrupt(delay):
            if probe.console_directory is not None:
                running.append((probe.process, Path(probe.console_directory.name)))
                raise KeyboardInterrupt
            time.sleep(delay)
        with self.assertRaises(KeyboardInterrupt):
            wait_bootloader("iPhone1,2", probe, timeout=3, sleep=interrupt,
                            emit=lambda *a: None, output=lambda *a, **k: None)
        self.assertEqual(len(running), 1)
        process, directory = running[0]
        self.assertIsNotNone(process.poll())
        self.assertFalse(directory.exists())
        self.assertIsNone(probe.process)
        self.assertIsNone(probe.console_directory)
        self.assertIsNone(probe.console_output)

    def test_timeout_partial_console_is_discarded_and_child_reaped(self):
        code = "import time; print(" + repr(IBEC_CONSOLE) + ",flush=True); time.sleep(30)"
        probe = RecoveryProbe([sys.executable, "-B", "-c", code])
        with patch("ios_timeout.recovery_timing.CONSOLE_READ_TIMEOUT", 0.15):
            with self.assertRaisesRegex(ConsoleReadError, "timed out"):
                while probe.bootloader_console() is None:
                    time.sleep(0.01)
        self.assertIsNone(probe.process)
        self.assertIsNone(probe.console_directory)

    def test_failed_or_oversized_console_cannot_authorize_upload(self):
        for code in ("import sys; print(" + repr(IBEC_CONSOLE) + "); sys.exit(9)",
                     "print(" + repr(IBEC_CONSOLE) + "); print('x'*70000)"):
            probe = RecoveryProbe([sys.executable, "-B", "-c", code])
            with self.subTest(code=code), self.assertRaises(ConsoleReadError):
                while probe.bootloader_console() is None:
                    time.sleep(0.01)
            self.assertIsNone(probe.process)
            self.assertIsNone(probe.console_directory)

    def test_late_matching_console_cannot_authorize_upload(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        def late():
            clock.sleep(1.1)
            return IBEC_CONSOLE
        probe.bootloader_console = late
        events = []
        with self.assertRaisesRegex(ResetError, "verified iBEC console"):
            wait_bootloader("iPhone1,2", probe, stage="ibec", timeout=1, clock=clock,
                            sleep=clock.sleep, emit=lambda *a: events.append(a), output=lambda *a, **k: None)
        self.assertFalse(any(e[0] == "Recovery bootloader detected" for e in events))
        self.assertTrue(probe.closed)

    def test_incomplete_query_retries_but_wrong_hardware_stops_before_console(self):
        clock = Clock()
        probe = Probe(clock)
        probe.poll = Mock(side_effect=["MODE: Recovery\nCPID: ???\n", STOCK.replace("DFU", "Recovery")])
        wait_bootloader("iPhone1,2", probe, timeout=1, clock=clock,
                        sleep=clock.sleep, emit=lambda *a: None, output=lambda *a, **k: None)
        self.assertEqual(probe.poll.call_count, 2)
        for snapshot in (STOCK.replace("8900", "8920"), STOCK.replace("04", "00"),
                         STOCK + "BDID: 04\n"):
            clock = Clock()
            probe = Probe(clock, ready_at=0, result=snapshot.replace("DFU", "Recovery"))
            probe.bootloader_console = Mock(return_value=IBSS_CONSOLE)
            with self.subTest(snapshot=snapshot), self.assertRaises(ResetError):
                wait_bootloader("iPhone1,2", probe, timeout=1, clock=clock,
                                sleep=clock.sleep, emit=lambda *a: None, output=lambda *a, **k: None)
            self.assertEqual(clock(), 0)
            probe.bootloader_console.assert_not_called()
            self.assertTrue(probe.closed)

    def test_wrong_console_board_stops_instead_of_retrying(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        probe.bootloader_console = Mock(side_effect=[IBSS_CONSOLE.replace("n82ap", "m68ap"), IBSS_CONSOLE])
        with self.assertRaisesRegex(ResetError, "board does not match"):
            wait_bootloader("iPhone1,2", probe, clock=clock, sleep=clock.sleep,
                            emit=lambda *a: None, output=lambda *a, **k: None)
        self.assertEqual(clock(), 0)
        self.assertEqual(probe.bootloader_console.call_count, 1)
        self.assertTrue(probe.closed)

    def test_mixed_console_banners_cannot_authorize_upload(self):
        clock = Clock()
        probe = Probe(clock, ready_at=0, result=STOCK.replace("DFU", "Recovery"))
        probe.bootloader_console = Mock(return_value=IBEC_CONSOLE + IBSS_CONSOLE)
        events = []
        with self.assertRaisesRegex(ResetError, "verified iBEC console"):
            wait_bootloader("iPhone1,2", probe, stage="ibec", timeout=0.2, clock=clock,
                            sleep=clock.sleep, emit=lambda *a: events.append(a), output=lambda *a, **k: None)
        self.assertFalse(any(e[0] == "Recovery bootloader detected" for e in events))

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
        with self.assertRaisesRegex(ResetError, "still in DFU.*ramdisk was not uploaded") as failure:
            wait_bootloader("iPhone1,2", probe, timeout=1, clock=clock,
                            sleep=clock.sleep, emit=lambda *a: events.append(a),
                            output=lambda value, **k: lines.append(value))
        self.assertAlmostEqual(clock(), 1)
        self.assertTrue(probe.closed)
        self.assertTrue(all(event[0] == "Waiting for iBSS to start" for event in events))
        self.assertIn("MODE=DFU", "\n".join(lines))
        self.assertNotIn("private-id", str(events) + str(lines))
        self.assertNotIn("hub", str(failure.exception))
        self.assertIn("not yet known", str(failure.exception))
        self.assertIn("force restart", str(failure.exception))

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

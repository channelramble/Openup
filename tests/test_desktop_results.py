import unittest

from ios_timeout.core import JOURNAL, PREFS
from ios_timeout.desktop_results import MAX_DETAIL_CHARS, MAX_DETAILS, summarize
from ios_timeout.backend_ui import RECOVERY_HANDOFF_LINE, RECOVERY_HANDOFF_TITLE


INSPECTION = f"""
iPhone 3G · iPhone1,2 · iOS 4.2.1 (8C148)
USB identifier: private-device-identifier
  Reset failed-attempt count to 0 and blocked state to false.
  Remove known cached timer/state entries; preserve other preferences.
  The correct passcode is still required. Future incorrect attempts can lock it again.
  Erase-after-failed-attempts is enabled and will remain enabled.
  update plist: {PREFS}
  original SHA-256: {'a' * 64}
  archive journal: {JOURNAL}
Preview only. No lockout files were changed.
"""


class DesktopResultTests(unittest.TestCase):
    def test_detected_variant_mismatch_is_actionable_without_terminal(self):
        result = summarize("recover", """
* Device: iPhone 5/5C (iPhone5,1)
* Mode: DFU
Connected device is iPhone5,1, but --model selects iPhone5,2.
[Error] Device identity verification failed.
Error: Upstream backend stopped with status 1.
""", 1)
        self.assertEqual(result["title"], "Phone model does not match")
        self.assertIn("iPhone5,1", result["message"])
        self.assertIn("Phone model", result["message"])
        self.assertIn("iPhone5,2", "\n".join(result["details"]))
        normal = summarize("recover", "[Error] Connected device is iPhone5,1, but --model selects iPhone5,2.", 2)
        self.assertEqual(normal["title"], "Phone model does not match")
        self.assertIn("iPhone5,1", normal["message"])
        interrupted = summarize("recover", "Connected device is iPhone5,1, but --model selects iPhone5,2.", 1, interrupted=True)
        self.assertIn("Stopped at your request", interrupted["message"])

    def test_inspection_lists_actual_changes_and_preserves_relevant_notice(self):
        result = summarize("inspect", INSPECTION, 0)
        joined = "\n".join(result["details"])
        self.assertIn("Review the proposed changes", result["message"])
        self.assertIn("iPhone 3G · iOS 4.2.1 (8C148)", joined)
        self.assertIn(PREFS, joined)
        self.assertIn(JOURNAL, joined)
        self.assertIn("Erase-after-failed-attempts", joined)
        self.assertIn("no lockout files were changed", joined)
        self.assertNotIn("private-device-identifier", joined)
        self.assertNotIn("original SHA-256", joined)

    def test_no_change_result_does_not_claim_a_backup(self):
        result = summarize(
            "reset", "Recognized state is already reset; no file changes are needed.\nNo changes made.\n", 0)
        self.assertIn("without changing files", result["message"])
        self.assertNotIn("backup", str(result).lower())

    def test_verified_reset_preserves_backup_but_not_claimed_screen_clearance(self):
        result = summarize("reset", """
Backup ready: /temporary/backup-1
Plist reset committed and read-back verified. Backup: /temporary/backup-1
On-screen timer clearance remains unverified until the phone boots.
Reboot request sent. Enter the correct passcode when the phone starts.
""", 0)
        self.assertIn("verified", result["message"])
        self.assertIn("Check the lockout screen", result["message"])
        self.assertIn("Backup: /temporary/backup-1", result["details"])
        self.assertIn("timer clearance is not confirmed", "\n".join(result["details"]))

    def test_failure_preserves_recovery_instructions_and_specific_backend_error(self):
        result = summarize("reset", """
[Error] Device transfer failed.
Error: Lost USB connection.
Recovery is required; keep the phone in its ramdisk.
Backup/recovery directory: /temporary/recovery-1
""", 1)
        self.assertIn("stopped", result["message"])
        self.assertIn("Recovery is required; keep the phone in its ramdisk.", result["details"])
        self.assertIn("Recovery backup: /temporary/recovery-1", result["details"])
        self.assertIn("Error: Lost USB connection.", result["details"])

    def test_interrupted_overrides_successful_exit_and_keeps_existing_evidence(self):
        result = summarize("reset", "Backup ready: /temporary/backup-1\n", 0, interrupted=True)
        self.assertIn("Stopped at your request", result["message"])
        self.assertIn("Backup created: /temporary/backup-1", result["details"])

    def test_doctor_is_a_readable_checklist_without_raw_paths_or_noise(self):
        result = summarize("doctor", """
Host: Darwin 27.0 / arm64
Python: 3.13.16
Default state/backups: /temporary/backups
USB transport endpoint: /var/run/usbmuxd
git: not installed (only needed to obtain a backend)
SSH library: Paramiko 5.1.0
SSH library: missing; run ./Launch.command or python3 -m pip install .
Native USB SSH: Apple Mobile Device Service is required.
random debug command with fixture-secret
""", 0)
        joined = "\n".join(result["details"])
        self.assertIn("Git: missing", joined)
        self.assertIn("Backups folder: /temporary/backups", joined)
        self.assertIn("Repair or reinstall", joined)
        self.assertNotIn("pip install", joined)
        self.assertNotIn("fixture-secret", str(result))

    def test_restore_and_finalize_require_explicit_verified_outcomes(self):
        for action, line in (
            ("restore", "Original files restored and verified. Backup: /temporary/backup-1"),
            ("finalize", "Transaction verified and lock cleanup complete. Backup: /temporary/backup-1"),
        ):
            with self.subTest(action=action):
                self.assertIn("No verified", summarize(action, "", 0)["message"])
                result = summarize(action, line, 0)
                self.assertNotIn("No verified", result["message"])
                self.assertIn("Backup: /temporary/backup-1", result["details"])

    def test_recovery_reports_only_exact_four_or_six_digit_results(self):
        output = "\n".join((
            "found : 0123", "found : 123456", "found : 0123",
            "found : 123", "found : 1234567", "echo found : 9999",
            "password: fixture-password", "token=fixture-token",
        ))
        result = summarize("recover", output, None)
        self.assertIn("reported a passcode", result["message"])
        self.assertEqual(result["details"], [
            "Reported passcode: 0123", "Reported passcode: 123456",
        ])
        self.assertEqual(result["passcode"], "123456")
        self.assertNotIn("0123", result["message"])
        self.assertNotIn("fixture-password", str(result))
        self.assertEqual(summarize("inspect", output, 0)["details"], [])
        self.assertNotIn("passcode", summarize("inspect", output, 0))
        self.assertIn("No passcode was reported", summarize("recover", "Booting...\n", 0)["message"])

    def test_patcher_probe_failures_are_not_operation_errors(self):
        result = summarize("prepare", """
Failed to find MOV.W R0, #0x0!
Failed to find RSA check; adjusting offset.
download succeeded
""", None)
        self.assertEqual(result["details"], [])
        self.assertEqual(result["message"], "The operation is still running.")

    def test_recovery_handoff_points_to_the_phone_without_claiming_a_passcode(self):
        result = summarize("recover", RECOVERY_HANDOFF_LINE + "\n", 0)
        self.assertEqual(result["title"], RECOVERY_HANDOFF_TITLE)
        self.assertIn("iPhone screen", result["message"])
        self.assertIn("no further menu choice", result["message"])
        self.assertNotIn("No passcode was reported", result["message"])
        self.assertNotIn("passcode", result)
        self.assertIn("not confirmed", "\n".join(result["details"]))

    def test_failed_or_interrupted_handoff_preserves_the_error_result(self):
        for code, interrupted in ((1, False), (0, True), (None, False)):
            with self.subTest(code=code, interrupted=interrupted):
                result = summarize("recover", RECOVERY_HANDOFF_LINE, code, interrupted=interrupted)
                self.assertNotEqual(result["title"], RECOVERY_HANDOFF_TITLE)
                self.assertNotIn("no further menu choice", result["message"])
        result = summarize("recover", RECOVERY_HANDOFF_LINE + "\n[Error] Fixture error", 0)
        self.assertNotEqual(result["title"], RECOVERY_HANDOFF_TITLE)
        self.assertIn("Error: Fixture error", result["details"])

    def test_handoff_is_not_inferred_from_ordinary_boot_output_or_other_actions(self):
        self.assertNotEqual(summarize("recover", "Boot commands finished.\n", 0)["title"],
                            RECOVERY_HANDOFF_TITLE)
        self.assertNotEqual(summarize("prepare", RECOVERY_HANDOFF_LINE, 0)["title"],
                            RECOVERY_HANDOFF_TITLE)

    def test_details_are_bounded_and_unrecognized_output_is_not_copied(self):
        output = "unrecognized progress\n" * 70_000
        output += "\n".join("Error: " + str(index) + "x" * 800 for index in range(30))
        output += "\nError: final error\x00\n"
        result = summarize("inspect", output, 1)
        self.assertLessEqual(len(result["details"]), MAX_DETAILS)
        self.assertTrue(all(len(value) <= MAX_DETAIL_CHARS + len("Error: ")
                            for value in result["details"]))
        self.assertIn("Error: final error", result["details"])
        self.assertNotIn("progress", str(result))
        self.assertNotIn("\x00", str(result))


if __name__ == "__main__":
    unittest.main()

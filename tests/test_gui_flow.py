import unittest

from ios_timeout.gui_flow import ActionCopy, TimerFlow, action_copy


class TimerFlowTests(unittest.TestCase):
    def test_prepare_includes_inspection_but_never_automatically_applies(self):
        flow = TimerFlow()
        self.assertEqual(flow.stage, "prepare")
        self.assertFalse(flow.can_apply)
        flow.start("prepare")
        self.assertFalse(flow.can_apply)
        flow.finish("prepare", 0)
        self.assertEqual(flow.stage, "review")
        self.assertTrue(flow.can_apply)
        flow.start("reset", apply=True)
        flow.finish("reset", 0, apply=True)
        self.assertEqual(flow.stage, "complete")
        self.assertFalse(flow.can_apply)

    def test_already_prepared_phone_can_be_inspected_directly(self):
        flow = TimerFlow()
        flow.start("inspect")
        flow.finish("inspect", 0)
        self.assertTrue(flow.can_apply)

    def test_reinspection_or_preparation_discards_previous_review(self):
        for action in ("prepare", "inspect"):
            with self.subTest(action=action):
                flow = TimerFlow()
                flow.finish("inspect", 0)
                flow.start(action)
                self.assertEqual(flow.stage, "prepare")
                self.assertFalse(flow.can_apply)
                flow.finish(action, 1)
                self.assertFalse(flow.can_apply)

    def test_failed_or_interrupted_reset_requires_inspection_again(self):
        for exit_code in (1, 130, -15):
            with self.subTest(exit_code=exit_code):
                flow = TimerFlow()
                flow.finish("inspect", 0)
                flow.start("reset", apply=True)
                flow.finish("reset", exit_code, apply=True)
                self.assertEqual(flow.stage, "prepare")
                self.assertFalse(flow.can_apply)

    def test_preview_never_marks_reset_complete(self):
        flow = TimerFlow()
        flow.finish("reset", 0)
        self.assertEqual(flow.stage, "review")
        self.assertTrue(flow.can_apply)

    def test_explicit_prepare_and_apply_can_complete(self):
        flow = TimerFlow()
        flow.finish("prepare", 0, apply=True)
        self.assertEqual(flow.stage, "complete")
        self.assertFalse(flow.can_apply)

    def test_selection_changes_and_other_device_work_can_invalidate(self):
        for stage_action, apply in (("inspect", False), ("reset", True)):
            with self.subTest(stage_action=stage_action):
                flow = TimerFlow()
                flow.finish(stage_action, 0, apply=apply)
                flow.invalidate()
                self.assertEqual(flow.stage, "prepare")
                self.assertFalse(flow.can_apply)

    def test_unrelated_operations_neither_unlock_nor_discard_review(self):
        for action in ("doctor", "devices", "bruteforce", "restore", "finalize"):
            with self.subTest(action=action):
                flow = TimerFlow()
                flow.finish(action, 0)
                self.assertFalse(flow.can_apply)
                flow.finish("inspect", 0)
                flow.start(action)
                flow.finish(action, 1)
                self.assertTrue(flow.can_apply)


class ActionCopyTests(unittest.TestCase):
    def test_every_worker_action_has_complete_user_facing_copy(self):
        for action in ("doctor", "devices", "prepare", "inspect", "reset",
                       "restore", "finalize", "bruteforce"):
            for apply in (False, True):
                with self.subTest(action=action, apply=apply):
                    copy = action_copy(action, apply=apply)
                    self.assertIsInstance(copy, ActionCopy)
                    for value in (copy.title, copy.running, copy.success, copy.failure):
                        self.assertIsInstance(value, str)
                        self.assertTrue(value.strip())
                        self.assertLessEqual(len(value), 70, value)

    def test_preview_and_apply_are_distinct_for_writing_operations(self):
        for action in ("prepare", "reset", "restore", "finalize", "bruteforce"):
            with self.subTest(action=action):
                preview = action_copy(action)
                applied = action_copy(action, apply=True)
                self.assertNotEqual(preview.title, applied.title)
                self.assertNotEqual(preview.success, applied.success)

    def test_download_is_not_described_as_running_recovery(self):
        copy = action_copy("bruteforce", install_only=True)
        self.assertIn("Download", copy.title)
        self.assertIn("downloaded and checked", copy.success)
        for value in (copy.title, copy.running, copy.success, copy.failure):
            self.assertLessEqual(len(value), 70, value)
        with self.assertRaises(ValueError):
            action_copy("bruteforce", install_only=True, apply=True)

    def test_success_does_not_claim_a_recovered_passcode_or_cleared_timer(self):
        recovery = action_copy("bruteforce", apply=True)
        self.assertIn("session ended", recovery.success)
        self.assertIn("result", recovery.success)
        self.assertNotIn("passcode found", recovery.success.lower())
        reset = action_copy("reset", apply=True)
        self.assertEqual(reset.success, "Reset operation finished")
        self.assertNotIn("timer cleared", reset.success.lower())

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            action_copy("not-an-operation")


if __name__ == "__main__":
    unittest.main()

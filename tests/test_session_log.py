import unittest

from ios_timeout.session_log import OMISSION_MARKER, SessionLog


class SessionLogTests(unittest.TestCase):
    def test_complete_transcript_preserves_chunks_and_unicode(self):
        log = SessionLog(max_bytes=512)
        parts = ["App version\n", "First failure\n", "Progress: ", "50% 🌍", "\nLast line\n"]
        for part in parts:
            log.append(part)
        self.assertEqual(log.text(), "".join(parts))
        self.assertNotIn(OMISSION_MARKER, log.text())

    def test_cap_retains_initial_context_and_recent_tail_with_explicit_marker(self):
        log = SessionLog(max_bytes=512)
        initial = "App version\nAction: prepare\nFirst failure\n"
        log.append(initial)
        for index in range(100):
            log.append("Progress line %d\n" % index)
        log.append("Latest failure\n")
        result = log.text()
        self.assertTrue(result.startswith(initial))
        self.assertTrue(result.endswith("Latest failure\n"))
        self.assertEqual(result.count(OMISSION_MARKER), 1)
        self.assertLessEqual(len(result.encode("utf-8")), 512)
        self.assertNotIn("Progress line 50\n", result)

    def test_single_oversized_chunk_and_unicode_boundaries_stay_bounded(self):
        log = SessionLog(max_bytes=513)
        log.append("Header\n" + "🌍é" * 1000 + "\nLatest\n")
        self.assertTrue(log.text().startswith("Header\n"))
        self.assertTrue(log.text().endswith("\nLatest\n"))
        self.assertNotIn("\ufffd", log.text())
        self.assertLessEqual(len(log.text().encode("utf-8")), 513)
        for _ in range(100):
            log.append("é🌍")
        self.assertTrue(log.text().endswith("é🌍"))
        self.assertNotIn("\ufffd", log.text())
        self.assertLessEqual(len(log.text().encode("utf-8")), 513)

    def test_clear_removes_context_tail_and_omission_state(self):
        log = SessionLog(max_bytes=512)
        log.append("Old context\n" + "x" * 1000)
        self.assertIn(OMISSION_MARKER, log.text())
        log.clear()
        self.assertEqual(log.text(), "")
        log.append("New operation\n")
        self.assertEqual(log.text(), "New operation\n")

    def test_many_tiny_appends_have_bounded_chunk_overhead_before_and_after_cap(self):
        log = SessionLog(max_bytes=128 * 1024)
        log.append("Initial header\n")
        for _ in range(70_000):
            log.append("x")
        self.assertNotIn(OMISSION_MARKER, log.text())
        self.assertLessEqual(len(log._chunks), 3)
        for _ in range(80_000):
            log.append("é🌍")
        result = log.text()
        self.assertTrue(result.startswith("Initial header\n"))
        self.assertTrue(result.endswith("é🌍"))
        self.assertEqual(result.count(OMISSION_MARKER), 1)
        self.assertNotIn("\ufffd", result)
        self.assertLessEqual(len(result.encode("utf-8")), log.max_bytes)
        self.assertLessEqual(len(log._chunks), 3)
        log.clear()
        self.assertEqual(len(log._chunks), 0)
        self.assertEqual(log.text(), "")


if __name__ == "__main__":
    unittest.main()

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from ios_timeout.backend_ui import adapt_prompts


BRUTE = r'''
print() { echo "${color_B}${1}${color_N}"; }
input() { echo "${color_Y}[Input] ${1}${color_N}"; }
pause() { input "Press Enter to continue (Ctrl+C to cancel)"; read -s; }
'''
LEGACY = r'''
print() {
    echo "${color_B}${1}${color_N}"
}
input() {
    echo "${color_Y}[Input] ${1}${color_N}"
}
pause() {
    input "Press Enter/Return to continue (or press Ctrl+C to cancel)"
    read -s
}
select_option() {
    if [[ $menu_old == 1 ]]; then
        select opt in "$@"; do
            selected=$((REPLY-1))
            break
        done
        return $selected
    fi
    return 99
}
'''


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "POSIX Bash is required (WSL on Windows)")
class BackendUiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bl-ios-ui-hooks-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.trace = self.root / "events.jsonl"
        self.python = self.root / "emit with spaces"
        emit_code = (
            "import json, os, sys\n"
            "with open(os.environ['BL_TEST_TRACE'], 'a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('TEST_UI_EVENT')\n"
        )
        self.python.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable)
            + " -c " + shlex.quote(emit_code) + ' "$@"\n'
        )
        self.python.chmod(0o700)

    def run_shell(self, backend, invocation, *, enabled=True, reply=""):
        source = adapt_prompts(BRUTE if backend == "recover" else LEGACY, backend)
        result = subprocess.run(
            [shutil.which("bash"), "--noprofile", "--norc"],
            input=source + "\n" + invocation + "\n", text=True, capture_output=True,
            env={**os.environ, "BL_IOS_UI_PROTOCOL": "1" if enabled else "",
                 "BL_IOS_PYTHON": str(self.python), "BL_TEST_TRACE": str(self.trace),
                 "BL_TEST_REPLY": reply}, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotRegex(
            result.stderr.lower(),
            r"bad interpreter|cannot execute: required file not found",
            result.stdout + result.stderr,
        )
        events = ([json.loads(line)[2:] for line in self.trace.read_text().splitlines()]
                  if self.trace.exists() else [])
        return result, events

    def test_cli_mode_keeps_original_text_and_emits_no_events(self):
        for backend in ("recover", "legacy"):
            with self.subTest(backend=backend):
                result, events = self.run_shell(
                    backend, "input 'Main Menu'\nprint '* Hold TOP and HOME buttons...'", enabled=False)
                self.assertEqual(events, [])
                self.assertEqual(result.stdout, "[Input] Main Menu\n* Hold TOP and HOME buttons...\n")

    def test_nonblocking_input_label_is_progress_not_a_prompt(self):
        _, events = self.run_shell("legacy", "input 'Main Menu'")
        self.assertEqual(events, [["progress", "Recovery tool", "Main Menu"]])

    def test_read_prompt_marker_survives_command_substitution_with_quoted_text(self):
        prompt = "Enter build's identifier: "
        result, events = self.run_shell(
            "recover", 'value="$(input ' + shlex.quote(prompt) + ')"; printf "%s\\n" "$value"')
        self.assertEqual(events, [["prompt", "auto", "Recovery tool", prompt]])
        self.assertIn("TEST_UI_EVENT\n[Input] " + prompt, result.stdout)

    def test_pause_is_an_explicit_continue_request_and_still_reads_enter(self):
        result, events = self.run_shell(
            "recover", "pause <<<'\n'\nprintf 'AFTER_PAUSE\\n'")
        self.assertEqual(events[0][:3], ["prompt", "continue", "Continue when ready"])
        self.assertIn("AFTER_PAUSE", result.stdout)

    def test_legacy_choices_preserve_labels_and_numeric_return_value(self):
        choices = ["Connect to SSH", "Quoted 'name'; $(do-not-execute)", "Exit"]
        invocation = "select_option " + " ".join(map(shlex.quote, choices))
        invocation += " <<< '2'\nprintf 'SELECTED:%s\\n' \"$?\""
        result, events = self.run_shell("legacy", invocation)
        self.assertIn("SELECTED:1", result.stdout)
        self.assertEqual(events[0][:4], [
            "prompt", "choice", "Choose the next step", "Choose an option to continue.",
        ])
        self.assertEqual(events[0][4:], [
            "--option", choices[0], "1", "--option", choices[1], "2", "--option", choices[2], "3",
        ])

    def test_dfu_phase_events_use_actual_phase_durations(self):
        invocation = r'''
device_dfuhelper() {
    local sec=8
    print "* Hold TOP and HOME buttons."
    print "* Release TOP button and keep holding HOME button."
}
bl_ios_early_prepare() {
    print "* Hold TOP and HOME for 10 seconds, then release TOP and keep holding HOME."
    print "* Release HOME when the app detects WTF mode."
    print "* Hold TOP and HOME buttons..."
    print "* Release TOP, keep holding HOME..."
    print "* USB entry mode detected. Release both buttons now."
}
device_dfuhelper
bl_ios_early_prepare
'''
        _, events = self.run_shell("recover", invocation)
        self.assertEqual([event[-1] for event in events], ["8", "8", "10", "15", "0"])
        self.assertTrue(all(event[:2] == ["progress", "Enter DFU mode"] for event in events[:-1]))
        self.assertEqual(events[-1][:2], ["progress", "Phone detected"])

    def test_exact_source_structure_is_required(self):
        with self.assertRaises(ValueError):
            adapt_prompts("input() { :; }", "recover")
        with self.assertRaises(ValueError):
            adapt_prompts(BRUTE, "unknown")
        with self.assertRaises(ValueError):
            adapt_prompts(adapt_prompts(BRUTE, "recover"), "recover")


if __name__ == "__main__":
    unittest.main()

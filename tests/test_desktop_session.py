import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ios_timeout.desktop_protocol import (
    EventDecoder, MarkerDecoder, MARKER_START, MARKER_END, PromptRecognizer, SecretRedactor, encode_interrupt,
    encode_raw, encode_reply, marker,
)
from ios_timeout.desktop_session import SessionProtocol


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.sent = []
        self.bridge = SessionProtocol(self.events.append, self.sent.append)

    def settle(self):
        self.bridge.tick(self.bridge.last_output + 0.2)

    def test_json_and_marker_frames_survive_every_chunk_boundary(self):
        event = {"type": "prompt", "kind": "text", "title": "Choose", "message": "A café?"}
        frame = json.dumps(event, ensure_ascii=False) + "\n"
        for index in range(len(frame) + 1):
            with self.subTest(index=index):
                parser = EventDecoder()
                self.assertEqual(parser.feed(frame[:index]) + parser.feed(frame[index:]), [event])
        frame = "before" + marker(event) + "after"
        for index in range(len(frame) + 1):
            with self.subTest(marker_index=index):
                parser = MarkerDecoder()
                parts = parser.feed(frame[:index]) + parser.feed(frame[index:], final=True)
                self.assertEqual([value for kind, value in parts if kind == "event"], [event])
                self.assertEqual("".join(value for kind, value in parts if kind == "output"), "beforeafter")

    def test_backend_marker_tolerates_console_wraps_and_cursor_sequences(self):
        event = {"type": "prompt", "kind": "text", "title": "Response",
                 "message": "A longer choice label with several words. " * 10}
        encoded = marker(event)[len(MARKER_START):-len(MARKER_END)]
        wrapped = "\r\n\x1b[0m".join(encoded[index:index + 60] for index in range(0, len(encoded), 60))
        self.assertEqual(MarkerDecoder().feed(MARKER_START + wrapped + MARKER_END),
                         [("event", event)])

    def test_password_is_redacted_across_echo_chunks_and_replies_are_not_logged(self):
        self.bridge.output(b"Ramdisk SSH root password: ")
        self.settle()
        prompt = self.bridge.pending
        self.assertEqual(prompt["kind"], "password")
        self.bridge.input(encode_reply(prompt["id"], "fixture-secret"))
        self.assertEqual(self.sent, [b"fixture-secret\n"])
        for part in ("echo: fixture-", "sec", "ret\n"):
            self.bridge.output(part)
        self.bridge.finish(0)
        transcript = "".join(event.get("text", "") for event in self.events)
        self.assertNotIn("fixture-secret", json.dumps(self.events))
        self.assertIn("echo: [redacted]", transcript)

    def test_stale_duplicate_and_invalid_menu_replies_never_reach_backend(self):
        self.bridge.output("1) First choice\n2) Second choice\n#? ")
        self.settle()
        question = self.bridge.pending
        self.assertEqual(question["kind"], "choice")
        self.bridge.input(encode_reply("stale", "1"))
        self.bridge.input(encode_reply(question["id"], "99"))
        self.assertFalse(self.sent)
        self.bridge.input(encode_reply(question["id"], "2"))
        self.bridge.input(encode_reply(question["id"], "1"))
        self.assertEqual(self.sent, [b"2\n"])
        self.assertIsNone(self.bridge.pending)

    def test_blank_continue_and_raw_response_invalidate_pending_prompt(self):
        self.bridge.output("Press Enter/Return to continue (Ctrl+C to cancel)\n")
        question = self.bridge.pending
        self.bridge.input(encode_reply(question["id"], ""))
        self.assertEqual(self.sent, [b"\n"])
        self.bridge.output("New question: ")
        self.settle()
        question = self.bridge.pending
        self.bridge.input(encode_raw(""))
        self.bridge.input(encode_reply(question["id"], "late"))
        self.assertEqual(self.sent, [b"\n", b"\n"])
        self.assertIsNone(self.bridge.pending)

    def test_instrumented_menu_is_not_replaced_by_shell_select_text(self):
        question = {"type": "prompt", "kind": "choice", "title": "Ramdisk ready",
                    "message": "Choose Exit to continue inspection.",
                    "options": [{"label": "Inspect files", "value": "1"},
                                {"label": "Continue inspection", "value": "2"}]}
        self.bridge.output(marker(question))
        prompt_id = self.bridge.pending["id"]
        self.bridge.output("1) Inspect files\n2) Exit\n#? ")
        self.settle()
        self.assertEqual(self.bridge.pending["id"], prompt_id)
        self.assertEqual(self.bridge.pending["message"], question["message"])
        self.bridge.input(encode_reply(prompt_id, "2"))
        self.assertEqual(self.sent, [b"2\n"])

    def test_delayed_visible_prompt_after_instrumented_answer_is_not_a_second_request(self):
        question = {"type": "prompt", "kind": "continue", "title": "Ready?",
                    "message": "Press Enter to continue:"}
        self.bridge.output("DFU Mode Helper\nGet ready to enter DFU mode.\n")
        self.bridge.output(marker(question))
        prompt_id = self.bridge.pending["id"]
        self.bridge.output("[Input] Press Enter to continue:\n")
        self.assertEqual(self.bridge.pending["id"], prompt_id)
        self.bridge.input(encode_reply(prompt_id, ""))
        self.bridge.output("Press Enter to continue:")
        self.settle()
        self.assertIsNone(self.bridge.pending)
        # A new explicit marker is an intentional retry, even with identical text.
        self.bridge.output(marker(question))
        self.assertNotEqual(self.bridge.pending["id"], prompt_id)

    def test_completed_file_labels_are_not_prompts_and_progress_clears_unknown_tail(self):
        self.bridge.output("Firmware files:\nRestoreRamdisk:\nKernelcache:\n")
        self.assertIsNone(self.bridge.pending)
        self.bridge.output("Unexpected upstream question: ")
        self.settle()
        self.assertEqual(self.bridge.pending["kind"], "text")
        self.bridge.output("\n* Hold TOP and HOME buttons...\n10 ")
        self.assertIsNone(self.bridge.pending)
        self.bridge.output("9 ")
        progress = [item for item in self.events if item["type"] == "progress"]
        self.assertEqual(progress[-1]["remaining"], 9)
        self.assertIn("TOP and HOME", progress[-1]["message"])

    def test_shell_numeric_choice_and_external_authentication_prompts(self):
        recognizer = PromptRecognizer()
        recognizer.feed("  1. iOS 6.1.3\n  2. iOS 9.0.2 [default]\n  3. Enter Build Number\n")
        recognizer.feed("[Input] Enter choice [1/2/3]: ")
        question = recognizer.idle()[-1]
        self.assertEqual([item["value"] for item in question["options"]], ["1", "2", "3"])
        for message in ("[sudo] password for fixture: ", "root@localhost's password: ",
                        "Enter passphrase for key '/unused/key': "):
            with self.subTest(message=message):
                self.assertEqual(PromptRecognizer().classify(message)["kind"], "password")
        question = PromptRecognizer().classify("Are you sure you want to continue connecting (yes/no/[fingerprint])? ")
        self.assertEqual(question["options"], [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])

    def test_control_injection_is_rejected_and_interrupt_is_idempotent(self):
        for value in ("one\ntwo", "one\rtwo", "\x03", "\x00", "\x1b[A"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                encode_raw(value)
        self.bridge.output("Enter a value: ")
        self.settle()
        prompt_id = self.bridge.pending["id"]
        self.bridge.input(json.dumps({"type": "reply", "id": prompt_id, "value": "one\ntwo"}) + "\n")
        self.assertFalse(self.sent)
        self.bridge.input(encode_interrupt())
        self.bridge.input(encode_interrupt())
        self.bridge.input(encode_reply(prompt_id, "late"))
        self.assertEqual(self.sent, [b"\x03"])

    def test_partial_log_chunks_never_become_input_requests(self):
        text = (
            "::\nFirmware files:\nRestoreRamdisk:\nECID:\npatch_rsa_check:\n"
            "find_dtre_get_value_bl_insn:\n[Input] Press Enter to continue (Ctrl+C to cancel)\n"
        )
        for offset in range(0, len(text), 7):
            self.bridge.output(text[offset:offset + 7])
            self.bridge.tick(self.bridge.last_output + 0.1)
        self.settle()
        prompts = [event for event in self.events if event["type"] == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["kind"], "continue")

    def test_redactor_masks_a_secret_prefix_at_end_of_stream(self):
        redactor = SecretRedactor()
        redactor.add("abcdef")
        self.assertEqual(redactor.feed("prefix abc"), "prefix ")
        self.assertEqual(redactor.feed("", final=True), "[redacted]")

    def test_wsl_protocol_flag_preserves_existing_environment_entries(self):
        from ios_timeout.wsl import desktop_wsl_environment
        with patch.dict(os.environ, {"BL_IOS_UI_PROTOCOL": "1", "WSLENV": "EXISTING/p:BL_IOS_UI_PROTOCOL/l:OTHER/u"}):
            environment = desktop_wsl_environment()
        self.assertEqual(environment["WSLENV"], "EXISTING/p:OTHER/u:BL_IOS_UI_PROTOCOL/u")
        self.assertEqual(environment["BL_IOS_UI_PROTOCOL"], "1")
        with patch.dict(os.environ, {"WSLENV": "EXISTING/p"}, clear=True):
            self.assertEqual(desktop_wsl_environment(), {"WSLENV": "EXISTING/p"})

    def test_bundled_wsl_launch_forwards_protocol_to_the_linux_cli(self):
        from ios_timeout import wsl
        from ios_timeout.cli import parser
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="/mnt/c/fixture\n")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wsl-runtime/bin").mkdir(parents=True)
            (root / "wsl-runtime/bin/python3").write_bytes(b"fixture")
            with patch.dict(os.environ, {"BL_IOS_DESKTOP": "1", "BL_IOS_UI_PROTOCOL": "1",
                                         "BL_IOS_APP_ROOT": str(root)}), \
                 patch.object(wsl.shutil, "which", return_value="wsl.exe"), \
                 patch.object(wsl.subprocess, "run", side_effect=run):
                args = parser().parse_args(["prepare", "--model", "iPhone1,2"])
                self.assertEqual(wsl.run_in_wsl(["prepare", "--model", "iPhone1,2"], args), 0)
        command, keywords = calls[-1]
        self.assertIn("--cli", command)
        self.assertEqual(keywords["env"]["BL_IOS_UI_PROTOCOL"], "1")
        self.assertIn("BL_IOS_UI_PROTOCOL/u", keywords["env"]["WSLENV"].split(":"))

    def test_conpty_adapter_uses_the_same_framed_replies_and_secret_masking(self):
        from ios_timeout import desktop_session as session
        incoming = queue.Queue()
        events = []
        received = []
        replied = threading.Event()

        class Process:
            exitstatus = 0
            alive = True
            reads = 0

            def isalive(self):
                return self.alive

            def write(self, text):
                received.append(text)
                replied.set()

            def read(self, _size):
                self.reads += 1
                if self.reads == 1:
                    return "Fixture password: "
                if self.reads == 2:
                    if not replied.wait(3):
                        raise AssertionError("ConPTY did not receive the reply.")
                    return "echo=conpty-secret\r\n"
                self.alive = False
                raise EOFError()

            def close(self):
                self.alive = False
                incoming.put(b"")

        process = Process()

        def emit(_fd, data):
            event = json.loads(data)
            events.append(event)
            if event["type"] == "prompt":
                incoming.put(encode_reply(event["id"], "conpty-secret"))

        module = SimpleNamespace(PtyProcess=SimpleNamespace(spawn=lambda _argv, **_kwargs: process))
        with patch.dict(sys.modules, {"winpty": module}), \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(session.sys, "platform", "win32"), \
             patch.object(session.os, "read", side_effect=lambda _fd, _size: incoming.get(timeout=3)), \
             patch.object(session, "_write_all", side_effect=emit):
            self.assertEqual(session.run_command(["fixture.exe"], structured=True), 0)
        self.assertIn("conpty-secret\r", received)
        self.assertNotIn("conpty-secret", json.dumps(events))
        self.assertIn("echo=[redacted]", "".join(event.get("text", "") for event in events))
        self.assertEqual(events[-1], {"type": "result", "code": 0})


@unittest.skipIf(os.name == "nt", "The real POSIX PTY fixture runs on macOS/Linux; ConPTY has a separate adapter.")
class RealTerminalTests(unittest.TestCase):
    def run_dialogue(self, script, respond):
        root = Path(__file__).resolve().parents[1]
        runner = (
            "import sys\n"
            "from ios_timeout.desktop_session import run_command\n"
            "raise SystemExit(run_command([sys.executable, '-u', '-c', " + repr(script) + "], structured=True))\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", runner], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        incoming = queue.Queue()
        events = []

        def reader():
            for line in iter(process.stdout.readline, b""):
                incoming.put(line)
            incoming.put(None)

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                try:
                    line = incoming.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    self.fail("The session emitted an invalid protocol frame: " + repr(line))
                events.append(event)
                if event["type"] == "prompt":
                    replies = respond(event)
                    for reply in replies:
                        process.stdin.write(reply)
                        process.stdin.flush()
            else:
                self.fail("The simulated terminal did not finish before the deadline.")
            code = process.wait(timeout=3)
            return code, events
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

    @unittest.skipUnless(shutil.which("bash"), "Bash is required for the select fixture.")
    def test_real_pty_continue_select_password_and_dfu_countdown(self):
        script = """
import getpass, subprocess, time
from ios_timeout.desktop_protocol import prompt
prompt('continue', 'Continue', 'Press Enter to continue:')
input('Press Enter to continue:')
print('\\n* Hold TOP and HOME buttons...')
for number in (10, 9):
    print(str(number) + ' ', end='', flush=True)
    time.sleep(0.03)
print('\\n* Release TOP, keep holding HOME...')
print('8 ', flush=True)
subprocess.run(['bash', '-c', 'PS3=\"#? \"; select choice in First Second; do echo \"selected=$choice\"; break; done'], check=True)
password = getpass.getpass('Ramdisk SSH root password: ')
print('deliberate echo=' + password)
print('fixture complete')
"""
        seen = []

        def respond(event):
            seen.append(event["kind"])
            value = {"continue": "", "choice": "2", "password": "fixture-private-secret"}[event["kind"]]
            return [encode_reply("old-id", "unused"),
                    encode_reply(event["id"], value),
                    encode_reply(event["id"], value)]

        code, events = self.run_dialogue(script, respond)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["continue", "choice", "password"])
        output = "".join(event.get("text", "") for event in events)
        self.assertIn("selected=Second", output)
        self.assertIn("fixture complete", output)
        self.assertIn("deliberate echo=[redacted]", output)
        self.assertNotIn("fixture-private-secret", json.dumps(events))
        self.assertTrue(any(event["type"] == "progress" and event.get("remaining") == 9 for event in events))
        self.assertEqual(events[-1], {"type": "result", "code": 0})

    def test_real_pty_interrupt_finishes_without_an_answer(self):
        code, events = self.run_dialogue(
            "input('Enter a fixture value: ')\nprint('must not reach')\n",
            lambda _event: [encode_interrupt()],
        )
        self.assertNotEqual(code, 0)
        self.assertNotIn("must not reach", "".join(event.get("text", "") for event in events))
        self.assertEqual(events[-1]["type"], "result")

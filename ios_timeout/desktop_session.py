"""Bridge backend terminals to structured desktop interaction or ordinary pipes."""

import codecs
import errno
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

from .desktop_model import TerminalText
from .desktop_protocol import (
    EventDecoder, MarkerDecoder, PROMPT_KINDS, PromptRecognizer, SecretRedactor,
    _reply_value, clean_line, encode_event,
)


def command(arguments):
    root = Path(os.environ.get("BL_IOS_APP_ROOT", Path(__file__).resolve().parent.parent))
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return [str(executable), "-I", "-B", "-u", str(root / "desktop.py"), "--cli", *arguments]


_CONTROL_C = r"(?:\b(?:ctrl|control)\s*(?:[+-]\s*)?c\b|\^c\b)"


def _gui_message(text, *, kind=None):
    """Translate terminal actions only on the desktop's guided surfaces.

    Classify prompts before this step so an Enter/path default stays an input
    request. Raw terminal output and CLI text remain useful diagnostic evidence.
    """
    # Closing a terminal is not a desktop action; Stop already requests the
    # same graceful interrupt without bypassing the app's cleanup lifecycle.
    text = re.sub("(" + _CONTROL_C + r")\s+or\s+close\s+the\s+terminal(?:\s+window)?",
                  r"\1", text, flags=re.I)

    def click(label):
        return lambda match: ("Click " if match.group(0)[0].isupper() else "click ") + label

    text = re.sub(r"\b(?:press|hit)\s+" + _CONTROL_C, click("Stop"), text, flags=re.I)
    text = re.sub(_CONTROL_C + r"(?=\s+to\s+(?:cancel|quit|exit|stop|abort)\b)",
                  "click Stop", text, flags=re.I)
    if kind == "continue":
        text = re.sub(r"\b(?:press|hit)\s+(?:the\s+)?(?:enter(?:/return)?|return|any key)\b",
                      click("Continue"), text, flags=re.I)
        text = re.sub(r"(click Continue)\s+to\s+continue\b", r"\1 when ready", text, flags=re.I)
    text = re.sub(r"(click Stop)\s+to\s+(?:quit|exit)\b", r"\1 to cancel", text, flags=re.I)
    text = re.sub(r"\bTo stop and exit,\s+(?:just\s+)?click Stop\b",
                  "To stop this operation, click Stop", text, flags=re.I)
    return text


class SessionProtocol:
    """Stateful, serialized prompt/reply handling shared by PTY and ConPTY."""

    def __init__(self, emit, send):
        self.emit = emit
        self.send = send
        self.lock = threading.RLock()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.input_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.inputs = EventDecoder()
        self.markers = MarkerDecoder()
        self.recognizer = PromptRecognizer()
        self.terminal = TerminalText()
        self.redactor = SecretRedactor()
        self.pending = None
        self.pending_instrumented = False
        self.answered_marker = None
        self.sequence = 0
        self.interrupted = False
        self.last_output = time.monotonic()
        self.closed = False

    def _done(self):
        if self.pending is not None:
            self.emit({"type": "prompt_done", "id": self.pending["id"]})
            self.pending = None

    def _event(self, event, instrumented=False):
        if event.get("type") == "progress":
            self._done()
            remaining = event.get("remaining")
            self.emit({"type": "progress", "title": str(event.get("title", "Working"))[:200],
                       "message": _gui_message(str(event.get("message", "")))[:6000],
                       "remaining": remaining if isinstance(remaining, int) and 0 <= remaining <= 3600 else None})
        elif event.get("type") == "prompt":
            if event.get("kind") == "auto":
                candidate = self.recognizer.classify(str(event.get("message", "")))
                if candidate is None:
                    self._event({"type": "progress", "title": "Working", "message": event.get("message", "")})
                    return
                event = candidate
            if event.get("kind") not in PROMPT_KINDS:
                return
            question = {"type": "prompt", "kind": event["kind"],
                        "title": str(event.get("title", "The tool needs your response"))[:200],
                        "message": _gui_message(str(event.get("message", "")), kind=event["kind"])[:6000],
                        "options": [],
                        "secret": event["kind"] == "password" or bool(event.get("secret", False))}
            options = event.get("options", [])
            if not isinstance(options, list):
                options = []
            for option in options[:32]:
                if not isinstance(option, dict) or not isinstance(option.get("value"), str):
                    continue
                try:
                    _reply_value(option["value"])
                except ValueError:
                    continue
                question["options"].append({"label": str(option.get("label", option["value"]))[:200],
                                            "value": option["value"]})
            if question["kind"] == "continue":
                question["options"] = [{"label": "Continue", "value": ""}]
            if question["kind"] == "choice" and not question["options"]:
                question["kind"] = "text"
            if (not instrumented and self.pending is not None and self.pending_instrumented
                    and self.pending["kind"] == "choice"
                    and (question["kind"] == "choice"
                         or question["message"] == "Choose one of the options shown by the tool.")):
                return
            if instrumented:
                self.answered_marker = None
            elif self.answered_marker is not None:
                old_kind, old_message = self.answered_marker
                new_message = clean_line(question["message"])
                if old_kind == question["kind"] and (
                        old_message == new_message or old_message.endswith("\n" + new_message)
                        or new_message.endswith("\n" + old_message)):
                    return
            # Instrumented prompts are usually followed by the same visible
            # read -p text. Preserve the existing ID rather than racing clicks.
            if self.pending is not None:
                old = clean_line(self.pending["message"])
                new = clean_line(question["message"])
                if (old == new or old.endswith("\n" + new) or new.endswith("\n" + old)) and self.pending["kind"] == question["kind"]:
                    return
                self._done()
            self.sequence += 1
            question["id"] = "p" + str(self.sequence)
            self.pending = question
            self.pending_instrumented = instrumented
            self.emit(question.copy())

    def _output(self, text, final=False):
        cleaned = self.terminal.feed(text.replace("\r\n", "\n").replace("\r", "\n"))
        visible = self.redactor.feed(cleaned, final=final)
        if visible:
            self.emit({"type": "output", "text": visible})
            for event in self.recognizer.feed(visible):
                self._event(event)

    def output(self, data):
        with self.lock:
            self.last_output = time.monotonic()
            text = self.decoder.decode(data) if isinstance(data, bytes) else data
            for kind, value in self.markers.feed(text):
                if kind == "event":
                    self._event(value, instrumented=True)
                else:
                    self._output(value)

    def tick(self, now=None):
        with self.lock:
            if self.closed:
                return
            if (time.monotonic() if now is None else now) - self.last_output >= 0.18:
                for event in self.recognizer.idle():
                    self._event(event)

    def input(self, data):
        with self.lock:
            text = self.input_decoder.decode(data) if isinstance(data, bytes) else data
            for event in self.inputs.feed(text):
                self._input_event(event)

    def _input_event(self, event):
        kind = event.get("type")
        if kind == "interrupt":
            if not self.interrupted:
                self.interrupted = True
                self._done()
                self.send(b"\x03")
            return
        if kind not in {"reply", "raw"} or self.interrupted:
            return
        question = self.pending
        if kind == "reply" and (question is None or event.get("id") != question["id"]):
            return  # A duplicate, delayed, or stale response must not reach read().
        try:
            value = _reply_value(event.get("value"))
        except ValueError:
            self.emit({"type": "progress", "title": "Response not sent",
                       "message": "Enter one response without terminal control characters.", "remaining": None})
            return
        if kind == "reply" and question["kind"] in {"choice", "continue"}:
            if value not in {item["value"] for item in question["options"]}:
                return
        if kind == "raw" or question["secret"]:
            self.redactor.add(value)
        if question is not None and self.pending_instrumented:
            self.answered_marker = (question["kind"], clean_line(question["message"]))
        self._done()
        self.recognizer.replied()
        self.send((value + "\n").encode("utf-8"))

    def finish(self, code):
        with self.lock:
            self.closed = True
            for kind, value in self.markers.feed(self.decoder.decode(b"", final=True), final=True):
                if kind == "output":
                    self._output(value)
                else:
                    self._event(value, instrumented=True)
            self._output("", final=True)
            self._done()
            self.emit({"type": "result", "code": code})


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def run_command(argv, *, structured=False):
    """Run an argument-vector command; exposed for device-free bridge tests."""
    os.environ["TERM"] = "dumb"
    os.environ["NO_COLOR"] = "1"
    if structured:
        os.environ["BL_IOS_UI_PROTOCOL"] = "1"
    if sys.platform == "win32":
        from winpty import PtyProcess
        process = PtyProcess.spawn(argv, dimensions=(40, 120))
        output_lock = threading.Lock()

        def emit(event):
            with output_lock:
                _write_all(sys.stdout.fileno(), encode_event(event))

        def send(data):
            process.write(data.decode("utf-8", "replace").replace("\n", "\r"))

        bridge = SessionProtocol(emit, send) if structured else None

        def input_loop():
            while process.isalive():
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    process.write("\x03")
                    break
                if bridge is not None:
                    bridge.input(data)
                else:
                    send(data)

        threading.Thread(target=input_loop, daemon=True).start()
        if bridge is not None:
            def tick_loop():
                while process.isalive():
                    time.sleep(0.05)
                    bridge.tick()
            threading.Thread(target=tick_loop, daemon=True).start()
        try:
            while True:
                text = process.read(4096)
                if text:
                    if bridge is not None:
                        bridge.output(text)
                    else:
                        sys.stdout.write(text)
                        sys.stdout.flush()
        except EOFError:
            pass
        finally:
            process.close()
        code = process.exitstatus if process.exitstatus is not None else 1
        if bridge is not None:
            bridge.finish(code)
        return code

    import pty
    import selectors
    import struct
    import termios
    import fcntl
    pid, master = pty.fork()
    if pid == 0:
        os.execv(argv[0], argv)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    if structured:
        # Prevent terminal line discipline from logging typed fields. Password
        # redaction also covers backends/ConPTY that restore or echo input.
        attributes = termios.tcgetattr(master)
        attributes[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(master, termios.TCSANOW, attributes)
    bridge = SessionProtocol(lambda event: _write_all(sys.stdout.fileno(), encode_event(event)),
                             lambda data: _write_all(master, data)) if structured else None

    def interrupt(_signum, _frame):
        try:
            if bridge is not None:
                bridge.input(encode_event({"type": "interrupt"}))
            else:
                os.write(master, b"\x03")
        except OSError:
            pass

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ, "output")
            try:
                selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "input")
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.EPERM):
                    raise
            finished = False
            while not finished:
                for key, _ in selector.select(timeout=0.05 if bridge is not None else None):
                    try:
                        data = os.read(key.fd, 16384)
                    except OSError as exc:
                        if exc.errno == errno.EIO and key.data == "output":
                            finished = True
                            break
                        raise
                    if not data:
                        if key.data == "output":
                            finished = True
                        else:
                            selector.unregister(key.fd)
                            interrupt(None, None)
                        continue
                    if bridge is not None:
                        (bridge.output if key.data == "output" else bridge.input)(data)
                    else:
                        target = sys.stdout.fileno() if key.data == "output" else master
                        _write_all(target, data)
                if bridge is not None:
                    bridge.tick()
    finally:
        os.close(master)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if bridge is not None:
        bridge.finish(code)
    return code


def main(arguments):
    return run_command(command(arguments), structured=os.environ.get("BL_IOS_UI_PROTOCOL") == "1")

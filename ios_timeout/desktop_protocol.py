"""Versioned, text-only interaction between desktop sessions and the GUI.

The outer pipe carries JSON lines. Optional backend instrumentation uses printable
base64 markers inside the PTY; printable delimiters also survive Windows ConPTY.
Neither protocol evaluates text, selects a menu option, nor answers a prompt.
"""

import argparse
import base64
import json
import os
import re
import sys

from .desktop_model import TerminalText

MAX_FRAME = 256 * 1024
MAX_REPLY = 4096
MARKER_START = "@@BL_IOS_UI:"
MARKER_END = "@@"
PROMPT_KINDS = {"continue", "choice", "password", "text"}


def encode_event(event):
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _reply_value(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_REPLY:
        raise ValueError("The response is too long.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Enter one response without terminal control characters.")
    return value


def encode_reply(prompt_id, value):
    if not isinstance(prompt_id, str) or not prompt_id:
        raise ValueError("A current prompt is required.")
    return encode_event({"type": "reply", "id": prompt_id, "value": _reply_value(value)})


def encode_interrupt():
    return encode_event({"type": "interrupt"})


def encode_raw(value):
    return encode_event({"type": "raw", "value": _reply_value(value)})


class EventDecoder:
    """Decode arbitrarily split JSON-line events without requiring Qt."""

    def __init__(self):
        self.pending = ""

    def feed(self, text):
        self.pending += text
        events = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                events.append(self._line(line))
        if len(self.pending) > MAX_FRAME:
            events.append({"type": "output", "text": self.pending})
            self.pending = ""
        return events

    @staticmethod
    def _line(line):
        try:
            event = json.loads(line)
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                return event
        except (ValueError, TypeError):
            pass
        # A failed worker/bootstrap may print an ordinary diagnostic.
        return {"type": "output", "text": line + "\n"}

    def finish(self):
        if not self.pending:
            return []
        line, self.pending = self.pending, ""
        return [self._line(line)]


def marker(event):
    payload = base64.b64encode(json.dumps(event, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return MARKER_START + payload + MARKER_END


def emit_event(event, stream=None):
    """Emit a backend marker only in a structured desktop operation."""
    if os.environ.get("BL_IOS_UI_PROTOCOL") != "1":
        return
    destination = stream if stream is not None else sys.stdout
    # Start in column zero so a console cannot wrap the marker's short prefix.
    framed = marker(event)
    destination.write("\n" + framed[:-len(MARKER_END)] + "\n" + MARKER_END + "\n")
    destination.flush()


def prompt(kind, title, message, options=None):
    emit_event({"type": "prompt", "kind": kind, "title": title, "message": message,
                "options": options or [], "secret": kind == "password"})


def progress(title, message, remaining=None):
    emit_event({"type": "progress", "title": title, "message": message, "remaining": remaining})


class MarkerDecoder:
    """Return ('output', text) / ('event', object) pairs, preserving order."""

    def __init__(self):
        self.pending = ""

    def feed(self, text, final=False):
        self.pending += text
        result = []
        while self.pending:
            start = self.pending.find(MARKER_START)
            if start < 0:
                keep = 0
                if not final:
                    for length in range(1, min(len(self.pending), len(MARKER_START) - 1) + 1):
                        if self.pending.endswith(MARKER_START[:length]):
                            keep = length
                end = len(self.pending) - keep
                if end:
                    result.append(("output", self.pending[:end]))
                    self.pending = self.pending[end:]
                break
            if start:
                result.append(("output", self.pending[:start]))
                self.pending = self.pending[start:]
            end = self.pending.find(MARKER_END, len(MARKER_START))
            if end < 0:
                if final or len(self.pending) > MAX_FRAME:
                    result.append(("output", self.pending))
                    self.pending = ""
                break
            frame, self.pending = self.pending[:end + len(MARKER_END)], self.pending[end + len(MARKER_END):]
            try:
                payload = TerminalText().feed(frame[len(MARKER_START):-len(MARKER_END)])
                # ConPTY may render long marker payloads across screen lines.
                data = base64.b64decode("".join(payload.split()), validate=True)
                event = json.loads(data.decode("utf-8"))
                if not isinstance(event, dict):
                    raise ValueError("Expected an event object.")
                result.append(("event", event))
            except (ValueError, UnicodeError):
                result.append(("output", frame))
        return result


def clean_line(text):
    return re.sub(r"^\s*(?:\[(?:Input|Log|WARNING)\]\s*|\*\s*)", "", text).strip()


class PromptRecognizer:
    """Fallback for uninstrumented read/select/getpass and external SSH/sudo.

    Recognition never sends input. Unknown questions and input-labelled prompts
    become ordinary GUI fields; a caller can also expose a manual response in
    Advanced without exposing the terminal in the normal workflow.
    """

    def __init__(self):
        self.terminal = TerminalText()
        self.tail = ""
        self.lines = []
        self.options = {}
        self.dfu_message = ""
        self.last_progress = None
        self.last_prompt = None

    def replied(self):
        self.tail = ""
        self.lines = []
        self.options = {}
        self.last_prompt = None

    def _options(self, line):
        pattern = r"(?:^|\s{2,})(\d+)[.)]\s+(.+?)(?=\s{2,}\d+[.)]\s+|$)"
        for match in re.finditer(pattern, line):
            value, label = match.groups()
            if len(self.options) < 32:
                self.options[value] = label.strip()

    def classify(self, text, *, generic=True):
        line = clean_line(text)
        lower = line.lower()
        if not line:
            return None
        kind, options, title = None, [], "The tool needs your response"
        if re.search(r"(?:password|passphrase|passcode)\b.*[:?]\s*$", lower):
            kind, title = "password", "Enter the requested password"
        elif re.search(r"(?:yes/no|y/n|y/N|Y/n)", line, re.I):
            kind = "choice"
            full_words = "yes/no" in lower
            options = [{"label": "Yes", "value": "yes" if full_words else "y"},
                       {"label": "No", "value": "no" if full_words else "n"}]
        elif re.search(r"(?:press|hit)\s+(?:the\s+)?(?:enter(?:/return)?|return|any key)\b", lower):
            # A text/path prompt may mention Enter as its cancellation option.
            if re.match(r"(?:enter|path|type)\b", lower):
                kind = "text"
            else:
                kind, title = "continue", "Ready to continue?"
                options = [{"label": "Continue", "value": ""}]
        elif re.search(r"#\?\s*$", line) or re.search(r"(?:select|choose|enter).*(?:option|choice).*[:?]\s*$", lower):
            if self.options:
                kind = "choice"
                options = [{"label": label, "value": value} for value, label in self.options.items()]
            else:
                values = re.search(r"\[(\d+(?:[/,-]\d+)+)\]", line)
                if values:
                    numbers = values.group(1)
                    if "-" in numbers and re.fullmatch(r"\d+-\d+", numbers):
                        first, last = map(int, numbers.split("-"))
                        values = [str(number) for number in range(first, min(last + 1, first + 32))]
                    else:
                        values = re.findall(r"\d+", numbers)[:32]
                    kind = "choice"
                    options = [{"label": value, "value": value} for value in values]
                else:
                    kind = "text"
        elif (re.match(r"(?:enter|type|provide|input|choose|select|apple id|path to)\b", lower)
              and re.search(r"[:?>]\s*$", line)):
            kind = "text"
        elif (generic and (line.endswith("?")
                           or re.search(r"[:>]\s*$", line) and " " in line
                           and not line.startswith(("http:", "https:")))):
            # Generic read -p prompts remain accessible even when new upstream
            # wording is not in the known inventory.
            kind = "text"
        if kind is None:
            return None
        if kind == "choice":
            title = "Choose an option"
        message = line if line != "#?" else "Choose one of the options shown by the tool."
        context = "\n".join(self.lines[-4:])
        if kind == "continue" and context and context != line:
            message = context + "\n" + line
        return {"type": "prompt", "kind": kind, "title": title, "message": message[:6000],
                "options": options, "secret": kind == "password"}

    def _progress(self, line):
        cleaned = clean_line(line)
        if re.search(r"\b(?:hold|release|press)\b.*\b(?:home|power|top|volume)\b", cleaned, re.I):
            self.dfu_message = cleaned
            return {"type": "progress", "title": "Prepare the phone for DFU",
                    "message": cleaned, "remaining": None}
        if self.dfu_message and re.fullmatch(r"(?:\d{1,3}\s+)*\d{1,3}\s*", cleaned):
            return {"type": "progress", "title": "Prepare the phone for DFU",
                    "message": self.dfu_message, "remaining": int(cleaned.split()[-1])}
        if re.match(r"(?:downloading|connecting|waiting|booting|sending|patching|finding|verifying|checking|extracting)\b",
                    cleaned, re.I):
            self.dfu_message = ""
            return {"type": "progress", "title": "Working", "message": cleaned[:2000], "remaining": None}
        return None

    def feed(self, text):
        # CR-only counters are separate updates, not concatenated status lines.
        self.tail += self.terminal.feed(text.replace("\r", "\n"))
        pieces = self.tail.split("\n")
        self.tail = pieces.pop()[-8192:]
        events = []
        for line, complete in [(line, True) for line in pieces] + [(self.tail, False)]:
            if not line.strip():
                continue
            self._options(line)
            # A read -p tail is recognized only after the session's short idle
            # debounce. A PTY chunk boundary is not a prompt boundary.
            event = self.classify(line, generic=False) if complete else None
            if event is not None:
                signature = (event["kind"], event["message"], json.dumps(event["options"], sort_keys=True))
                if signature != self.last_prompt:
                    events.append(event)
                    self.last_prompt = signature
            status = self._progress(line)
            if status is not None and status != self.last_progress:
                events.append(status)
                self.last_progress = status
            if complete:
                self.lines.append(clean_line(line))
                self.lines = self.lines[-8:]
        return events

    def idle(self):
        event = self.classify(self.tail)
        if event is None:
            return []
        signature = (event["kind"], event["message"], json.dumps(event["options"], sort_keys=True))
        if signature == self.last_prompt:
            return []
        self.last_prompt = signature
        return [event]


class SecretRedactor:
    """Mask submitted secrets even when PTY echo is split across reads."""

    def __init__(self):
        self.secrets = []
        self.pending = ""

    def add(self, value):
        if value and value not in self.secrets:
            self.secrets.append(value)
            self.secrets.sort(key=len, reverse=True)

    def feed(self, text, final=False):
        value = self.pending + text
        self.pending = ""
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        keep = 0
        if not final:
            for secret in self.secrets:
                for length in range(1, min(len(value), len(secret) - 1) + 1):
                    if value.endswith(secret[:length]):
                        keep = max(keep, length)
        if keep:
            self.pending = value[-keep:]
            value = value[:-keep]
        elif final and value and any(secret.startswith(value) for secret in self.secrets):
            value = "[redacted]"
        return value


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Emit a desktop interaction marker.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    question = subparsers.add_parser("prompt")
    question.add_argument("kind", choices=sorted(PROMPT_KINDS | {"auto"}))
    question.add_argument("title")
    question.add_argument("message")
    question.add_argument("--option", nargs=2, action="append", default=[])
    status = subparsers.add_parser("progress")
    status.add_argument("title")
    status.add_argument("message")
    status.add_argument("--remaining", type=int)
    args = parser.parse_args(arguments)
    if args.command == "prompt":
        prompt(args.kind, args.title, args.message,
               [{"label": label, "value": value} for label, value in args.option])
    else:
        progress(args.title, args.message, args.remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded result cards from recognized CLI output, never a second raw console.

The caller keeps the original transcript. This module only returns display
data; in particular it does not save or export reported recovery passcodes.
"""

from collections import deque
import re

from .core import JOURNAL, PREFS, SUPPORTED


MAX_CAPTURE_CHARS = 1024 * 1024
MAX_DETAIL_CHARS = 400
MAX_DETAILS = 16

_IDENTITY = re.compile(
    r"^(iPhone [^·]{1,32}) · (iPhone\d+,\d+) · iOS ([0-9.]{3,12}) "
    r"\(([A-Za-z0-9]{3,24})\)$"
)
_ERROR = re.compile(
    r"^(?:\[Error\]|Error:|Connection or operation failed:|"
    r"Boot component patch failed:|Kernel patch failed:)\s*(.+)$",
    re.IGNORECASE,
)
_NOTES = {
    "Reset failed-attempt count to 0 and blocked state to false.",
    "Remove known cached timer/state entries; preserve other preferences.",
    "The correct passcode is still required. Future incorrect attempts can lock it again.",
    "Erase-after-failed-attempts is enabled and will remain enabled.",
    "No LockoutStateJournal.plist exists; none will be created.",
    "Archive the old lockout journal so iOS can recreate it.",
    "Recovery is required; keep the phone in its ramdisk.",
    "No reset was committed; original files are intact.",
}
_BACKUPS = (
    ("Backup ready: ", "Backup created: "),
    ("Backup/recovery directory: ", "Recovery backup: "),
    ("Plist reset committed and read-back verified. Backup: ", "Backup: "),
    ("Original files restored and verified. Backup: ", "Backup: "),
    ("Transaction verified and lock cleanup complete. Backup: ", "Backup: "),
    ("Keep the phone in its ramdisk and run finalize on: ", "Finish cleanup using backup: "),
)


def _bounded(value):
    value = " ".join("".join(char for char in value if char.isprintable()).split())
    return value if len(value) <= MAX_DETAIL_CHARS else value[:MAX_DETAIL_CHARS - 1] + "…"


def summarize(action, capture, exit_code, interrupted=False):
    """Return ``{title, message, details}``; ``None`` exit means still running.

    Only explicit known outcomes affect result wording. A successful process
    exit alone never establishes timer clearance, a backup, or a passcode.
    The optional ``passcode`` is the latest exact backend-reported result,
    suitable for a separate session-only result label, not proof it works.
    """
    details = []
    errors = deque(maxlen=4)
    passcodes = deque(maxlen=4)
    no_changes = reset_verified = restore_verified = cleanup_verified = False
    preview = False

    def add(value):
        value = _bounded(value)
        if value and value not in details and len(details) < MAX_DETAILS - 4:
            details.append(value)

    text = capture[-MAX_CAPTURE_CHARS:]
    if len(capture) > MAX_CAPTURE_CHARS:
        # Discard the partial first line created by the input cap.
        text = text.partition("\n")[2]
    for raw in text.splitlines():
        # Long merged progress lines are not semantic output.
        if len(raw) > 4096:
            continue
        line = raw.strip()
        error = _ERROR.fullmatch(line)
        if error:
            value = _bounded(error[1])
            if value and value not in errors:
                errors.append(value)
            continue
        if action == "recover":
            found = re.fullmatch(r"found\s*:\s*([0-9]{4}|[0-9]{6})", line)
            if found and found[1] not in passcodes:
                passcodes.append(found[1])
        if action == "doctor":
            if line.startswith(("Host: ", "Python: ", "USB transport endpoint: ")):
                add(line)
            elif line.startswith("Default state/backups: "):
                add("Backups folder: " + line.removeprefix("Default state/backups: "))
            elif line.startswith("SSH library: Paramiko "):
                add(line)
            elif line.startswith("SSH library: missing"):
                add("SSH library: unavailable. Repair or reinstall the desktop app.")
            elif line.startswith("git: found"):
                add("Git: available for backend downloads.")
            elif line.startswith("git: not installed"):
                add("Git: missing; backend downloads require it.")
            elif line.startswith("Ramdisk preparation: WSL 2 ("):
                add(line)
            elif line == "Native USB SSH: Apple Mobile Device Service is required.":
                add(line)
            elif line == "Install/start usbmuxd and ensure USB device permissions (udev/sudo).":
                add("Linux USB: usbmuxd must be running with access to the phone.")
            elif line == "iPhone 4S pwnDFU requires external A5 hardware or a suitable existing kDFU setup.":
                add(line)
            elif line == "This release has not been validated on a physical phone.":
                add(line)
        if action not in {"prepare", "inspect", "reset", "restore", "finalize"}:
            continue
        identity = _IDENTITY.fullmatch(line)
        if identity and identity[2] in SUPPORTED and SUPPORTED[identity[2]][0] == identity[1]:
            add(f"{identity[1]} · iOS {identity[3]} ({identity[4]})")
        elif line in _NOTES:
            add(line)
        elif line == "update plist: " + PREFS:
            add("Update timer preferences: " + PREFS)
        elif line == "archive journal: " + JOURNAL:
            add("Archive lockout journal: " + JOURNAL)
        elif line in ("Recognized state is already reset; no file changes are needed.", "No changes made."):
            no_changes = True
            add("No file changes are needed.")
        elif line == "Preview only. No lockout files were changed.":
            preview = True
            add("Inspection only: no lockout files were changed.")
        elif line.startswith("Plist reset committed and read-back verified. Backup: "):
            reset_verified = True
            add("Reset files were written and read back successfully.")
        elif line.startswith("Original files restored and verified. Backup: "):
            restore_verified = True
            add("Original files were restored and verified.")
        elif line.startswith("Transaction verified and lock cleanup complete. Backup: "):
            cleanup_verified = True
            add("Transaction verified; cleanup is complete.")
        elif line == "On-screen timer clearance remains unverified until the phone boots.":
            add("Check the lockout screen after rebooting; timer clearance is not confirmed.")
        elif line == "Reboot request sent. Enter the correct passcode when the phone starts.":
            add("Reboot requested. Use the correct passcode when the phone starts.")
        elif line == "Phone left in its ramdisk. Reboot using the ramdisk tool when ready.":
            add("The phone is still in its recovery system. Reboot when ready.")
        for prefix, label in _BACKUPS:
            if line.startswith(prefix):
                add(label + line[len(prefix):])
                break

    for error in errors:
        details.append("Error: " + error)
    if passcodes:
        # Prioritize a reported result over lower-priority details, while
        # keeping it out of messages that might be used as ordinary status.
        details = details[:MAX_DETAILS - len(passcodes)]
        details += ["Reported passcode: " + code for code in passcodes]
    details = details[:MAX_DETAILS]
    titles = {
        "doctor": "Computer checks", "devices": "Phone connection",
        "prepare": "Phone preparation", "inspect": "Timer inspection",
        "reset": "Timer reset", "restore": "Restore originals",
        "finalize": "Transaction cleanup", "recover": "Passcode recovery",
    }
    title = titles.get(action, "Operation result")
    if interrupted:
        message = "Stopped at your request. Review any reported result before continuing."
    elif exit_code not in (None, 0):
        message = "The operation stopped before completion."
        if not errors:
            message += " No specific error was reported."
    elif exit_code is None:
        message = ("The backend reported a passcode. Verify it on the phone."
                   if passcodes else "The operation is still running.")
    elif action in {"prepare", "inspect"} or (action == "reset" and preview):
        message = ("Inspection finished. No file changes are needed." if no_changes else
                   "Inspection finished. Review the proposed changes before applying a reset.")
    elif action == "reset":
        if no_changes:
            message = "The operation finished without changing files."
        elif reset_verified:
            message = "Reset files were verified. Check the lockout screen after rebooting."
        else:
            message = "Reset operation ended. No verified result was reported."
    elif action == "restore":
        message = ("Original files were restored and verified." if restore_verified else
                   "Restore operation ended. No verified restore was reported.")
    elif action == "finalize":
        message = ("Transaction verified and cleanup completed." if cleanup_verified else
                   "Cleanup operation ended. No verified cleanup was reported.")
    elif action == "recover":
        message = ("The backend reported a passcode. Verify it on the phone." if passcodes else
                   "Recovery session ended. No passcode was reported in this session.")
    elif action == "doctor":
        message = "Computer checks finished. Review the requirements below."
    elif action == "devices":
        message = "Phone scan finished. Review the connected device list."
    else:
        message = "The operation finished."
    result = {"title": title, "message": message, "details": details}
    if passcodes:
        result["passcode"] = passcodes[-1]
    return result

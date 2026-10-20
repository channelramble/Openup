"""UI guidance based on explicit operation results, without reading terminal text."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionCopy:
    title: str
    running: str
    success: str
    failure: str


def action_copy(action, *, apply=False, install_only=False):
    """Describe the requested operation without promising a device outcome."""
    if action == "doctor":
        return ActionCopy(
            "Run diagnostics",
            "Checking this computer…",
            "Computer checks finished",
            "Diagnostics stopped — review the result",
        )
    if action == "devices":
        return ActionCopy(
            "Find connected phones",
            "Looking for connected phones…",
            "Phone scan finished",
            "Phone scan stopped — review the result",
        )
    if action == "prepare":
        return ActionCopy(
            "Prepare and reset" if apply else "Prepare and inspect",
            "Preparing the phone and applying its timer reset…" if apply else
            "Preparing the phone and checking its timer state…",
            "Preparation and reset operation finished" if apply else
            "Phone prepared and inspected",
            "Preparation stopped — review the result",
        )
    if action == "inspect" or (action == "reset" and not apply):
        return ActionCopy(
            "Inspect timer state",
            "Checking the phone and reviewing proposed changes…",
            "Inspection finished",
            "Inspection stopped — review the result",
        )
    if action == "reset":
        return ActionCopy(
            "Apply timer reset",
            "Backing up and applying the timer reset…",
            "Reset operation finished",
            "Reset stopped — review the result before retrying",
        )
    if action == "restore":
        return ActionCopy(
            "Restore original files" if apply else "Review restore",
            "Restoring and verifying the original files…" if apply else
            "Checking the backup and connected phone…",
            "Restore operation finished" if apply else "Restore preview finished",
            "Restore stopped — review the result",
        )
    if action == "finalize":
        return ActionCopy(
            "Finish transaction cleanup" if apply else "Review transaction cleanup",
            "Verifying the transaction and finishing cleanup…" if apply else
            "Checking the transaction and connected phone…",
            "Transaction cleanup finished" if apply else "Cleanup preview finished",
            "Transaction cleanup stopped — review the result",
        )
    if action == "recover":
        if install_only:
            if apply:
                raise ValueError("Choose either backend download or recovery.")
            return ActionCopy(
                "Download recovery backend",
                "Downloading and checking the recovery backend…",
                "Recovery backend downloaded and checked",
                "Backend download stopped — review the result",
            )
        if not apply:
            return ActionCopy(
                "Review recovery setup",
                "Reviewing recovery setup…",
                "Recovery setup preview finished",
                "Recovery setup preview stopped — review the result",
            )
        return ActionCopy(
            "Start passcode recovery",
            "Recovery session running — follow the on-screen steps",
            "Recovery session ended — review the result",
            "Recovery session stopped — review the result",
        )
    raise ValueError("Unknown desktop operation.")


class TimerFlow:
    """Guide the timer workflow; the backend still validates each operation.

    A successful prepare already includes inspection. A review is only valid
    for the current selection and connection settings, so the GUI must call
    invalidate() when those change or another workflow takes over the phone.
    """

    def __init__(self):
        self.stage = "prepare"

    @property
    def can_apply(self):
        return self.stage == "review"

    def invalidate(self):
        self.stage = "prepare"

    def start(self, action, *, apply=False):
        if action in ("prepare", "inspect"):
            self.invalidate()

    def finish(self, action, exit_code, *, apply=False):
        if action not in ("prepare", "inspect", "reset"):
            return
        if exit_code != 0:
            self.invalidate()
        elif apply and action in ("prepare", "reset"):
            self.stage = "complete"
        else:
            self.stage = "review"

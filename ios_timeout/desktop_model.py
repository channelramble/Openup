"""GUI-independent operation planning and backup inventory."""

from dataclasses import dataclass
from pathlib import Path

from .core import ResetError, load_backup


@dataclass
class Settings:
    udid: str = ""
    model: str = "iPhone1,2"
    port: int = 22
    mount: bool = True
    ask_password: bool = False
    trust_new_key: bool = False
    wsl: bool = False
    wsl_distro: str = ""
    ipsw: str = ""
    armv6_tools: str = ""
    kit: str = ""
    reboot: bool = False


def operation_arguments(action, settings, *, apply=False, backup=None, install_only=False):
    if action not in {"doctor", "devices", "inspect", "reset", "prepare", "restore", "finalize", "recover"}:
        raise ResetError("Unknown desktop operation.")
    args = [action]
    if action in {"restore", "finalize"}:
        if not backup:
            raise ResetError("Choose a backup transaction first.")
        args.append(str(Path(backup).expanduser().absolute()))
    if action in {"inspect", "reset", "prepare", "restore", "finalize"}:
        if settings.udid:
            args += ["--udid", settings.udid]
        args += ["--port", str(settings.port)]
        for enabled, flag in ((settings.mount, "--mount"), (settings.ask_password, "--ask-password"),
                              (settings.trust_new_key, "--trust-new-key"), (settings.wsl, "--wsl")):
            if enabled:
                args.append(flag)
        if apply and action != "inspect":
            args.append("--apply")
            if settings.reboot:
                args.append("--reboot")
        # Pass the selected model so inspect/reset can confirm an iPhone 3G in a
        # bare SSH ramdisk that serves no sysctl. restore/finalize derive the
        # model from the saved transaction instead.
        if settings.model and action in {"inspect", "reset", "prepare"}:
            args += ["--model", settings.model]
    if action == "prepare":
        for value, flag in ((settings.ipsw, "--ipsw"), (settings.kit, "--kit")):
            if value:
                args += [flag, value]
    if action == "recover":
        if settings.udid:
            args += ["--udid", settings.udid]
        if install_only and apply:
            raise ResetError("Choose either download or run.")
        if apply and settings.model == "iPhone1,2" and not settings.armv6_tools:
            from .armv6_payloads import read_bundled_payloads
            read_bundled_payloads()
        if settings.model:
            args += ["--model", settings.model]
        if settings.armv6_tools:
            args += ["--armv6-tools", settings.armv6_tools]
        if apply:
            args.append("--apply")
        if install_only:
            args.append("--install-only")
    if settings.wsl_distro and action != "doctor":
        args += ["--wsl-distro", settings.wsl_distro]
    if action == "devices" and settings.wsl:
        args.append("--wsl")
    return args


def backup_inventory(root):
    rows = []
    root = Path(root)
    if not root.exists():
        return rows
    for folder in sorted(root.iterdir(), reverse=True):
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            _, manifest, identity, _ = load_backup(folder)
            rows.append({"path": str(folder), "name": folder.name, "model": identity.model,
                         "version": identity.version, "status": manifest.get("status", "unknown"),
                         "valid": True})
        except (ResetError, OSError, ValueError) as exc:
            rows.append({"path": str(folder), "name": folder.name, "model": "—", "version": "—",
                         "status": "Invalid: " + str(exc), "valid": False})
    return rows


class TerminalText:
    """Strip terminal controls without allowing output to become rich text."""

    def __init__(self):
        self.pending = ""

    def feed(self, text):
        value = self.pending + text
        self.pending = ""
        out = []
        index = 0
        while index < len(value):
            char = value[index]
            if char == "\x1b":
                start = index
                index += 1
                if index == len(value):
                    self.pending = value[start:]
                    break
                if value[index] == "[":
                    index += 1
                    while index < len(value) and not ("@" <= value[index] <= "~"):
                        index += 1
                    if index == len(value):
                        self.pending = value[start:][-1024:]
                        break
                    index += 1
                elif value[index] == "]":
                    index += 1
                    while index < len(value) and value[index] != "\x07" and value[index:index + 2] != "\x1b\\":
                        index += 1
                    if index == len(value):
                        self.pending = value[start:][-1024:]
                        break
                    index += 1 if value[index] == "\x07" else 2
                else:
                    index += 1
            else:
                if char in "\n\t" or ord(char) >= 32:
                    out.append(char)
                index += 1
        return "".join(out)

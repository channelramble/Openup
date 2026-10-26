"""Run Unix-only ramdisk backends in WSL without invoking a shell."""

import shutil
import subprocess
import zipfile
import os
from importlib import resources
from pathlib import Path

from .core import ResetError
from .host import host_lock, state_directory

PATH_FLAGS = {"--kit", "--ipsw", "--backend-dir", "--armv6-tools", "--state-dir", "--backups"}
VALUE_FLAGS = PATH_FLAGS | {"--model", "--port", "--udid"}


def desktop_wsl_environment():
    """Carry desktop runtime requirements and protocol opt-in into WSL."""
    environment = os.environ.copy()
    for key in ("BL_IOS_UI_PROTOCOL", "BL_IOS_DESKTOP"):
        if environment.get(key) == "1":
            values = [item for item in environment.get("WSLENV", "").split(":")
                      if item and item.split("/", 1)[0] != key]
            # /u passes the scalar from Win32 to WSL, without path translation.
            environment["WSLENV"] = ":".join(values + [key + "/u"])
    return environment


def wsl_prefix(distribution=None):
    return ["wsl.exe"] + (["--distribution", distribution] if distribution else []) + ["--exec"]


def translated_arguments(arguments, translate):
    result = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        flag, equals, value = argument.partition("=")
        if flag == "--wsl-distro":
            index += 1 if equals else 2
            continue
        if flag == "--wsl":
            index += 1
            continue
        if flag in PATH_FLAGS:
            if not equals:
                index += 1
                value = arguments[index]
            result.extend([flag, translate(value)])
        elif flag in VALUE_FLAGS:
            result.append(argument)
            if not equals:
                index += 1
                result.append(arguments[index])
        elif (index > 0 and arguments[0] in ("restore", "finalize")
              and not argument.startswith("-")):
            result.append(translate(argument))
        else:
            result.append(argument)
        index += 1
    return result


def build_runner(path):
    package = resources.files("ios_timeout")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in package.iterdir():
            if item.name.endswith((".py", ".txt")):
                archive.writestr("ios_timeout/" + item.name, item.read_bytes())
        for item in package.joinpath("data").iterdir():
            if item.is_file():
                archive.writestr("ios_timeout/data/" + item.name, item.read_bytes())
        archive.writestr("__main__.py",
                         "from ios_timeout.launcher import main\nraise SystemExit(main())\n")


def run_in_wsl(arguments, options):
    if not shutil.which("wsl.exe"):
        raise ResetError("Install WSL 2 with a Linux distribution, then use usbipd-win to attach the phone.")
    prefix = wsl_prefix(options.wsl_distro)
    desktop_root = Path(os.environ.get("BL_IOS_APP_ROOT", "."))
    bundled_python = desktop_root / "wsl-runtime/bin/python3"
    bundled = os.environ.get("BL_IOS_DESKTOP") == "1" and bundled_python.is_file()
    available = None if bundled else subprocess.run(prefix + ["python3", "-c",
                                        "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"],
                               capture_output=True, text=True, timeout=30)
    if available is not None and available.returncode:
        raise ResetError("The selected WSL distribution needs Python 3.9+, python3-venv, Git, and Bash.")

    def translate(value):
        if value.startswith("/"):
            return value  # An explicitly supplied path inside the distribution.
        absolute = str(Path(value).expanduser().absolute())
        completed = subprocess.run(prefix + ["wslpath", "-a", "-u", absolute],
                                   capture_output=True, text=True, check=True, timeout=30)
        path = completed.stdout.strip()
        if not path.startswith("/") or "\n" in path:
            raise ResetError("WSL returned an invalid translated path.")
        return path

    if bundled:
        forwarded = translated_arguments(arguments, translate)
        print("Using the bundled Linux Python and SSH libraries inside WSL 2.", flush=True)
        print("The phone must be attached to WSL with usbipd; DFU transitions may require reattachment.", flush=True)
        return subprocess.run(prefix + [translate(str(bundled_python)), "-I", "-B", "-u",
                                       translate(str(desktop_root / "desktop.py")), "--cli"] + forwarded,
                              env=desktop_wsl_environment()).returncode

    directory = state_directory() / "wsl"
    with host_lock(directory):
        runner = directory / "runner.pyz"
        build_runner(runner)
        forwarded = translated_arguments(arguments, translate)
        print("Running the ramdisk workflow inside WSL 2. The phone must be attached there with usbipd.", flush=True)
        print("USB reconnection during DFU may require reattaching it. Windows and WSL use separate state directories.", flush=True)
        return subprocess.run(prefix + ["python3", translate(str(runner))] + forwarded,
                              env=desktop_wsl_environment()).returncode

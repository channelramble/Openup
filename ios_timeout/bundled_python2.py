"""Select the app-owned legacy interpreter without a machine-wide install."""

import json
import os
from pathlib import Path
import subprocess

from .core import ResetError
from .host import reject_link


VERSION = "2.7.18"


def app_root():
    # Also resolves correctly when Windows launches desktop.py through its
    # bundled Linux interpreter in WSL. Never trust a caller-supplied root.
    return Path(__file__).resolve().parent.parent


def environment(original=None):
    """Return a child environment whose python2 resolves inside this app."""
    result = dict(os.environ if original is None else original)
    runtime = app_root() / "python2-runtime"
    if not runtime.exists() and not runtime.is_symlink() and result.get("BL_IOS_DESKTOP") != "1":
        return result  # Source-only CLI retains its existing dependency setup.
    executable = runtime / "bin/python2"
    binary = runtime / "bin/python2.7"
    try:
        for path in (runtime, runtime / "bin", executable, binary):
            reject_link(path)
        if any(not path.is_file() or not os.access(path, os.X_OK) for path in (executable, binary)):
            raise OSError("Missing bundled interpreter")
    except OSError as exc:
        raise ResetError("Openup's bundled Python 2 runtime is missing or damaged. "
                         "Reinstall the complete Openup app.") from exc
    result["BL_IOS_PYTHON2"] = str(executable)
    result["PATH"] = str(executable.parent) + os.pathsep + result.get("PATH", os.defpath)
    try:
        checked = subprocess.run(
            [str(executable), "-c", "import ctypes,hashlib,json,struct,sys,zlib; "
             "print(json.dumps([list(sys.version_info[:3]),sys.prefix,struct.calcsize('P')]))"],
            env=result, cwd=runtime, capture_output=True, text=True, timeout=15, check=True,
        )
        if len(checked.stdout) > 4096:
            raise ValueError("Unexpected interpreter output")
        version, prefix, pointers = json.loads(checked.stdout)
        if version != [2, 7, 18] or Path(prefix).resolve() != runtime.resolve() or pointers != 8:
            raise ValueError("Unexpected bundled interpreter")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        raise ResetError("Openup's bundled Python 2 runtime could not start. "
                         "Reinstall the complete app for your computer.") from exc
    return result

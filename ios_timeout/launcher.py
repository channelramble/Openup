"""Dependency bootstrap shared by native launchers and the WSL bridge."""

import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .host import host_lock, state_directory
from . import __version__


def main():
    if sys.version_info < (3, 9):
        print("Python 3.9 or newer is required.", file=sys.stderr)
        return 1
    root = Path(__file__).parent.parent
    runtime = state_directory() / "runtime" / __version__
    python = runtime / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    env = {**os.environ, "PYTHONPATH": str(root)}
    try:
        from .updates import dispatch
        dispatched = dispatch(sys.argv[1:] or ["wizard"])
        if dispatched is not None:
            return dispatched
        with host_lock(runtime):
            if not python.exists():
                subprocess.run([sys.executable, "-m", "venv", str(runtime / "venv")], check=True)
            check = subprocess.run([str(python), "-m", "ios_timeout.dependencies"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode:
                lock = runtime / "requirements-lock.txt"
                lock.write_text(resources.files("ios_timeout").joinpath("runtime-requirements.txt").read_text())
                subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                                "--force-reinstall", "-r", str(lock)], check=True, env=env)
                subprocess.run([str(python), "-m", "ios_timeout.dependencies"], check=True, env=env)
        return subprocess.run([str(python), "-m", "ios_timeout"] + (sys.argv[1:] or ["wizard"]),
                              env=env).returncode
    except Exception as exc:
        print(f"Launcher failed: {exc}", file=sys.stderr)
        print("Linux/WSL also needs the python3-venv package.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

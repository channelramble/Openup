"""Entry point for the bundled desktop app and its isolated operation processes."""

import os
import sys
from pathlib import Path


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # Do not inherit user-installed Python packages or source-updater dispatch.
    os.environ["BL_IOS_DESKTOP"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    if (sys.platform.startswith("linux") and (root / "platform-libs").is_dir()
            and os.environ.get("BL_IOS_LIBRARIES_ROOT") != str(root)):
        old = os.environ.get("BL_IOS_LIBRARIES_ROOT", "")
        libraries = [value for value in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
                     if value and value != str(Path(old) / "platform-libs")]
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([str(root / "platform-libs"), *libraries])
        os.environ["BL_IOS_LIBRARIES_ROOT"] = str(root)
        os.execv(sys.executable, [sys.executable, "-I", "-B", str(root / "desktop.py"), *sys.argv[1:]])
    os.environ["BL_IOS_APP_ROOT"] = str(root)
    os.environ.setdefault("BL_IOS_ORIGINAL_ROOT", str(root))
    if sys.argv[1:2] == ["--cli"]:
        from ios_timeout.cli import main as cli
        return cli(sys.argv[2:])
    if sys.argv[1:2] == ["--session"]:
        from ios_timeout.desktop_session import main as session
        return session(sys.argv[2:])
    from ios_timeout.desktop_updates import dispatch
    if "--self-test" not in sys.argv and "--screenshot" not in sys.argv:
        dispatched = dispatch(root, sys.argv[1:])
        if dispatched is not None:
            return dispatched
    from ios_timeout.gui import main as gui
    return gui()


if __name__ == "__main__":
    raise SystemExit(main())

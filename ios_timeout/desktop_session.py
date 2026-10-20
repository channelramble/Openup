"""Bridge a real terminal to the GUI's pipes, including interactive backend prompts."""

import errno
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


def command(arguments):
    root = Path(os.environ.get("BL_IOS_APP_ROOT", Path(__file__).resolve().parent.parent))
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return [str(executable), "-I", "-B", "-u", str(root / "desktop.py"), "--cli", *arguments]


def main(arguments):
    os.environ["TERM"] = "dumb"
    os.environ["NO_COLOR"] = "1"
    if sys.platform == "win32":
        from winpty import PtyProcess
        process = PtyProcess.spawn(command(arguments), dimensions=(40, 120))

        def input_loop():
            while process.isalive():
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    process.write("\x03")
                    break
                process.write(data.decode("utf-8", "replace").replace("\n", "\r"))

        threading.Thread(target=input_loop, daemon=True).start()
        try:
            while True:
                text = process.read(4096)
                if text:
                    sys.stdout.write(text)
                    sys.stdout.flush()
        except EOFError:
            pass
        finally:
            process.close()
        return process.exitstatus if process.exitstatus is not None else 1

    import pty
    import selectors
    import struct
    import termios
    import fcntl
    pid, master = pty.fork()
    if pid == 0:
        os.execv(sys.executable, command(arguments))
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    def interrupt(_signum, _frame):
        try:
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
                for key, _ in selector.select():
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
                    target = sys.stdout.fileno() if key.data == "output" else master
                    view = memoryview(data)
                    while view:
                        view = view[os.write(target, view):]
    finally:
        os.close(master)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)

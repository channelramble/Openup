"""Build the app's local-only CPython 2.7 runtime on Ubuntu 22.04 x86_64."""

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

VERSION = "2.7.18"
URL = "https://www.python.org/ftp/python/2.7.18/Python-2.7.18.tar.xz"
SHA256 = "b62c0e7937551d0cc02b8fd5cb0f544f9405bafc9a54d3808ed4594812edef43"
ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/python2-runtime"
PROBE = """import ctypes, hashlib, json, os, struct, sys, zlib
assert sys.version_info[:3] == (2, 7, 18)
assert struct.calcsize('P') == 8
assert hashlib.sha256(b'openup').hexdigest() == 'bee3b55d3acd5f39ab4ca24605255b69540ffdf4b7a478cb9858f371eebfc948'
assert zlib.decompress(zlib.compress(b'openup')) == b'openup'
ctypes.CDLL(None)
print(json.dumps({'version':sys.version, 'prefix':sys.prefix, 'executable':sys.executable}))
"""


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data = urllib.request.urlopen(URL, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Python source checksum mismatch")
    source_archive = OUTPUT / "Python-2.7.18.tar.xz"
    source_archive.write_bytes(data)
    with tempfile.TemporaryDirectory(prefix="python2-build-", dir=OUTPUT) as temporary:
        temporary = Path(temporary)
        with tarfile.open(source_archive) as archive:
            archive.extractall(temporary, filter="data")
        source = temporary / "Python-2.7.18"
        setup = source / "setup.py"
        content = setup.read_text()
        original = "disabled_module_list = []"
        if content.count(original) != 1:
            raise ValueError("Unexpected Python module selection layout")
        disabled = ["_ssl", "_hashlib", "readline", "_sqlite3", "_tkinter", "_bsddb",
                    "dbm", "gdbm", "crypt", "_curses", "_curses_panel", "nis"]
        setup.write_text(content.replace(original, "disabled_module_list = " + repr(disabled)))
        runtime = temporary / "original-location/python2-runtime"
        env = {**os.environ, "CFLAGS": "-O2 -fcommon -Wno-implicit-function-declaration"}
        subprocess.run(["./configure", "--prefix=" + str(runtime), "--disable-shared",
                        "--without-ensurepip", "--with-system-ffi"], cwd=source, env=env, check=True)
        subprocess.run(["make", "-j4"], cwd=source, env=env, check=True)
        subprocess.run(["make", "install"], cwd=source, env=env, check=True)
        # Keep an interpreter and its standard library. Build headers, archives,
        # packaging tools and tests are not runtime dependencies.
        for name in ("include", "share", "lib/pkgconfig"):
            shutil.rmtree(runtime / name, ignore_errors=True)
        lib = runtime / "lib/python2.7"
        for name in ("test", "idlelib", "lib2to3/tests", "ensurepip", "config", "site-packages"):
            shutil.rmtree(lib / name, ignore_errors=True)
        for path in list(runtime.rglob("*")):
            if path.is_file() and path.suffix in (".pyc", ".pyo", ".a"):
                path.unlink()
        binary = runtime / "bin/python2.7"
        for path in list((runtime / "bin").iterdir()):
            if path != binary:
                path.unlink()
        (runtime / "LICENSE.Python").write_bytes((source / "LICENSE").read_bytes())
        # Bundle the three non-glibc dependencies. Module rpaths and each
        # library's own rpath resolve within the relocated app.
        native = runtime / "lib/native"
        native.mkdir()
        licenses = runtime / "licenses"
        licenses.mkdir()
        system = re.compile(r"^(?:ld-linux|linux-vdso|libc\.|libm\.|libdl\.|libpthread\.|librt\.|libutil\.)")
        expected = {"libffi.so.8": "libffi8", "libbz2.so.1.0": "libbz2-1.0", "libz.so.1": "zlib1g"}
        pending = [p for p in runtime.rglob("*") if p.is_file() and not p.is_symlink()
                   and p.open("rb").read(4) == b"\x7fELF"]
        while pending:
            path = pending.pop()
            listing = subprocess.check_output(["ldd", str(path)], text=True)
            for name, origin in re.findall(r"^\s*(\S+) => (/\S+)", listing, re.MULTILINE):
                if system.match(name):
                    continue
                if name not in expected:
                    raise ValueError("Unexpected Python runtime dependency: " + name)
                destination = native / name
                if not destination.exists():
                    shutil.copyfile(origin, destination)
                    shutil.copyfile(Path("/usr/share/doc") / expected[name] / "copyright",
                                    licenses / (expected[name] + ".copyright"))
                    pending.append(destination)
            relative = os.path.relpath(native, path.parent)
            subprocess.run(["patchelf", "--set-rpath", "$ORIGIN/" + relative, str(path)], check=True)
        # A separate shell entry can strip ambient Python configuration without
        # changing the app's Python 3 process environment.
        launcher = runtime / "bin/python2"
        launcher.write_text('#!/bin/sh\nset -eu\nbase=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                            'unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE\n'
                            'export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1\n'
                            'exec "$base/bin/python2.7" -E -s -B "$@"\n')
        launcher.chmod(0o755)
        relocated = temporary / "relocated app with spaces/python2-runtime"
        relocated.parent.mkdir()
        shutil.move(runtime, relocated)
        clean = {"PATH": "/usr/bin:/bin", "HOME": str(temporary / "empty-home"),
                 "PYTHONHOME": "/nonexistent", "PYTHONPATH": "/nonexistent"}
        smoke = subprocess.check_output([str(relocated / "bin/python2"), "-c", PROBE],
                                        env=clean, cwd="/", text=True)
        report = {"version": VERSION, "source_url": URL, "source_sha256": SHA256,
                  "target": "linux-x86_64", "relocated_smoke": json.loads(smoke)}
        members = {}
        for path in sorted(relocated.rglob("*")):
            if path.is_symlink():
                # libpython's optional duplicate aliases are not needed. Other
                # links must be flattened into the self-contained runtime.
                content = path.read_bytes()
                path.unlink()
                path.write_bytes(content)
            if path.is_file():
                relative = path.relative_to(relocated).as_posix()
                members[relative] = {"size": path.stat().st_size,
                                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                     "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644}
        destination = OUTPUT / "python2-2.7.18-linux-x86_64.zip"
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, item in members.items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | item["mode"]) << 16
                archive.writestr(info, (relocated / name).read_bytes())
        report.update({"archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                       "archive_size": destination.stat().st_size, "files": members})
        destination.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
        print(destination)


if __name__ == "__main__":
    main()

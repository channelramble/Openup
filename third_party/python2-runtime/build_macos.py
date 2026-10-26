"""Build and validate a relocatable, system-library-only CPython 2.7.18 runtime.

Maintainer build tool; no system installation, package manager or USB access.
Run on macOS with Xcode/CLT. Intel requires native Intel or existing Rosetta.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

RECIPE = Path(__file__).resolve().parent
REPO = RECIPE.parents[1]
VERSION = "2.7.18"
SOURCE_URL = "https://www.python.org/ftp/python/2.7.18/Python-2.7.18.tar.xz"
SOURCE_SHA256 = "b62c0e7937551d0cc02b8fd5cb0f544f9405bafc9a54d3808ed4594812edef43"
SOURCE_SIZE = 12854736
PYENV_REVISION = "0effba62c4bbf8f8ca2ad8f0eabbfd8221cc7bc6"
PATCHES = {
    "0001-Detect-arm64-in-configure.patch": "55ecbb6c1b8e2e851dcfed3b26a9d8269739af55ade8d164563875cab6d44a6b",
    "0002-Fix-macOS-_tkinter-use-of-Tck-Tk-in-Library-Framewor.patch": "de212642dc73941397b686a5b4310efc7f05147b6c72e46c8508f73a90adc056",
    "0003-Support-arm64-in-Mac-Tools-pythonw.patch": "a2ff3ba40f6eebb85dc58b369e6ee6274a5af8b1c2bb4b54e33bf8d4c980d0f5",
    "0004-Use-system-libffi-for-Mac-OS-10.15-and-up.patch": "c91dc6d66b1a5aca54eec3944e019708da925fe580e81475f3257bd11b08529a",
    "0005-ctypes-use-the-correct-ABI-for-variadic-functions.patch": "ad429a3d29bcb073b3d142534882b777b44d08b05a12e8b172747bbf8422fe53",
    "0006-ctypes-probe-libffi-for-ffi_closure_alloc-and-ffi_pr.patch": "e87c717a401c89e4518ea5ae68fb7ad8813a084af98fdfe31db2bc43efc2deaf",
    "0007-Remove-QuickTime-from-link-args.patch": "f2a3b73b1660f0443eb75f696660e989667da37728e56f0517ece892e5a6dccb",
}
DISABLED = ["_ssl", "_hashlib", "_tkinter", "readline", "bsddb", "_bsddb", "dbm",
            "_curses", "_curses_panel", "nis", "gdbm", "_sqlite3", "_testcapi", "_ctypes_test"]
WRAPPER = '''#!/bin/sh
# App-owned Python 2: do not inherit a user's unrelated Python installation.
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE
unset DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH DYLD_INSERT_LIBRARIES LD_LIBRARY_PATH LD_PRELOAD
HERE=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd) || exit 1
exec "$HERE/python2.7" -E -s -B "$@"
'''
SMOKE = r'''import bz2, ctypes, hashlib, json, os, struct, sys, zlib
root = os.path.realpath(sys.argv[1])
assert sys.version_info[:3] == (2, 7, 18)
assert struct.calcsize('P') == 8
assert os.path.realpath(sys.prefix) == root
assert os.path.realpath(sys.exec_prefix) == root
assert hashlib.sha256('abc').hexdigest() == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
assert zlib.decompress(zlib.compress('test')) == 'test'
assert bz2.decompress(bz2.compress('test')) == 'test'
callback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)(lambda value: value + 1)
assert callback(41) == 42
assert ctypes.CDLL('/usr/lib/libSystem.B.dylib').getpid() == os.getpid()
assert all(not p or os.path.realpath(p).startswith(root + os.sep) for p in sys.path)
print(json.dumps({'version':sys.version.split()[0], 'bits':struct.calcsize('P')*8,
                  'prefix':sys.prefix, 'ctypes_callback':True, 'zlib_bz2_hashlib':True}))
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_source(work):
    inputs = work / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    source = inputs / "Python-2.7.18.tar.xz"
    if not source.is_file() or source.stat().st_size != SOURCE_SIZE or digest(source) != SOURCE_SHA256:
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
            data = response.read(SOURCE_SIZE + 1)
        if len(data) != SOURCE_SIZE or hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
            raise ValueError("CPython source checksum mismatch")
        source.write_bytes(data)
    return source


def package(folder, source, arch, minimum):
    stage = folder / "python2-runtime"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "bin").mkdir(parents=True)
    shutil.copy2(folder / "installed/bin/python2.7", stage / "bin/python2.7", follow_symlinks=True)
    (stage / "bin/python2").write_text(WRAPPER)
    (stage / "bin/python2").chmod(0o755)
    shutil.copytree(folder / "installed/lib/python2.7", stage / "lib/python2.7",
                    ignore=shutil.ignore_patterns("*.pyc", "*.pyo", "*.a", "test", "tests", "config",
                                                  "idlelib", "lib-tk", "ensurepip", "site-packages"))
    shutil.copy2(source / "LICENSE", stage / "LICENSE.Python")
    shutil.copy2(RECIPE / "LICENSE.pyenv", stage / "LICENSE.pyenv")
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise ValueError("Runtime must contain no symlinks")
        if path.is_file() and (path.suffix == ".so" or path.name == "python2.7"):
            subprocess.run(["/usr/bin/strip", "-S", str(path)], check=True)
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True)
    sys.path.insert(0, str(REPO))
    from macos_compatibility import validate_macos_bundle
    compatibility = validate_macos_bundle(stage, arch, tuple(map(int, minimum.split("."))) + (0,))
    with tempfile.TemporaryDirectory(prefix="relocated python2 ", dir=folder) as temporary:
        relocated = Path(temporary) / "runtime with spaces"
        shutil.copytree(stage, relocated)
        hostile = Path(temporary) / "unrelated-python"
        hostile.mkdir()
        (hostile / "sitecustomize.py").write_text("raise RuntimeError('ambient Python was used')\n")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(hostile), "PYTHONHOME": str(hostile),
               "PYTHONPATH": str(hostile), "PYTHONUSERBASE": str(hostile), "PYTHONSTARTUP": str(hostile / "sitecustomize.py")}
        result = subprocess.check_output([str(relocated / "bin/python2"), "-c", SMOKE, str(relocated)],
                                         cwd=hostile, env=env, text=True, timeout=30)
        smoke = json.loads(result)
        smoke["prefix"] = "relocated runtime with spaces"
    files = {}
    archive_path = folder.parent / ("python2-" + VERSION + "-macos-" + arch + ".zip")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                name = path.relative_to(stage).as_posix()
                mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                data = path.read_bytes()
                info = zipfile.ZipInfo(name, (2020, 4, 20, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
                files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode}
    manifest = {"schema": 1, "version": VERSION, "target": "macos-" + arch,
                "archive_sha256": digest(archive_path), "size": archive_path.stat().st_size,
                "files": files, "source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256,
                "pyenv_revision": PYENV_REVISION, "patches": PATCHES, "disabled_modules": DISABLED,
                "validation": {"compatibility": compatibility, "relocation_smoke": smoke},
                "build_recipe_sha256": digest(Path(__file__).resolve())}
    archive_path.with_suffix(".json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"archive": str(archive_path), "sha256": manifest["archive_sha256"],
                      "size": manifest["size"], "files": len(files), "validation": manifest["validation"]}), flush=True)


def build(arch, work):
    if sys.platform != "darwin":
        raise ValueError("The macOS runtime must be built on macOS")
    work = work.resolve()
    source_archive = checked_source(work)
    minimum = "14.0" if arch == "arm64" else "12.0"
    folder = work / ("macos-" + arch)
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    with tarfile.open(source_archive) as archive:
        for member in archive.getmembers():
            if (member.name != "Python-2.7.18" and not member.name.startswith("Python-2.7.18/")) or member.issym() or member.islnk():
                raise ValueError("Unexpected CPython source member")
            if not (folder / member.name).resolve().is_relative_to(folder):
                raise ValueError("Invalid CPython source path")
        archive.extractall(folder)
    source = folder / "Python-2.7.18"
    for name, expected in PATCHES.items():
        patch = RECIPE / "patches" / name
        if digest(patch) != expected:
            raise ValueError("Changed compatibility patch")
        subprocess.run(["/usr/bin/patch", "-p1", "-i", str(patch)], cwd=source, check=True)
    setup = source / "setup.py"
    text = setup.read_text()
    assert "disabled_module_list = []" in text
    text = text.replace("disabled_module_list = []", "disabled_module_list = " + repr(DISABLED))
    for kind in ("library", "include"):
        suffix = "lib" if kind == "library" else "include"
        old = "add_dir_to_list(self.compiler.%s_dirs, '/usr/local/%s')" % (kind, suffix)
        assert old in text
        text = text.replace(old, "pass  # Do not discover developer-installed libraries")
    setup.write_text(text)
    sdk = subprocess.check_output(["/usr/bin/xcrun", "--show-sdk-path"], text=True).strip()
    flags = ("-O2 -arch " + arch + " -mmacosx-version-min=" + minimum + " -isysroot " + sdk
             + " -Wno-error=implicit-function-declaration -Wno-error=int-conversion"
             + " -Wno-error=incompatible-function-pointer-types")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(folder), "LC_ALL": "C",
           "MACOSX_DEPLOYMENT_TARGET": minimum, "CC": "/usr/bin/clang -arch " + arch,
           "CFLAGS": flags, "LDFLAGS": "-arch " + arch + " -mmacosx-version-min=" + minimum + " -isysroot " + sdk,
           "SDKROOT": sdk, "SOURCE_DATE_EPOCH": "1587340800"}
    configure = ["/bin/sh", "./configure", "--prefix=" + str(folder / "installed"),
                 "--with-system-ffi", "--without-ensurepip", "--disable-shared", "--enable-unicode=ucs4"]
    if arch == "x86_64":
        configure = ["/usr/bin/arch", "-x86_64"] + configure
    for name, command in (("configure", configure), ("make", ["/usr/bin/make", "-j8"]),
                          ("install", ["/usr/bin/make", "install"])):
        with (folder / (name + ".log")).open("w") as log:
            subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    package(folder, source, arch, minimum)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arch", choices=("arm64", "x86_64"))
    parser.add_argument("--work-dir", type=Path, default=REPO / "build/python2-runtime")
    args = parser.parse_args()
    build(args.arch, args.work_dir)

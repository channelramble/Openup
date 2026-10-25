"""Build only: original iPhone helper for the pinned 3.1.3 RAM disk."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--linker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve() / "openup-iphone2g"
    includes = root.parent / "iphone-dataprotection" / "include"
    command = ["clang", "-target", "armv6-apple-ios3.1", "-mcpu=arm1176jzf-s", "-marm",
               "-std=gnu99", "-Wall", "-Wextra", "-Werror", "-O2", "-isysroot", str(args.sdk.resolve()),
               "-I" + str(includes), "--ld-path=" + str(args.linker.resolve()), "-mlinker-version=609",
               "-Wl,-no_compact_unwind", "-Wl,-platform_version,ios,3.1,3.1.3",
               "-framework", "CoreFoundation", "-framework", "IOKit",
               str(root / "openup-iphone2g.c"), str(root / "bootstrap.c"), str(root / "ssh-owner.c"), "-o", str(output)]
    subprocess.run(command, check=True, env={**os.environ, "SOURCE_DATE_EPOCH": "1418119387"})
    subprocess.run(["codesign", "--force", "--sign", "-", "--digest-algorithm=sha1,sha256",
                    "--timestamp=none", str(output)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(output)], check=True)
    manifest = {"schema": 1, "product": "iPhone1,1", "architecture": "armv6",
                "ramdisk_build": "7E18", "minimum_ios": "3.1", "size": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "sources": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                            for name in ("openup-iphone2g.c", "bootstrap.c", "ssh-owner.c")}}
    def archive(path, entries):
        with zipfile.ZipFile(path, "w") as package:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name, (2026, 10, 25, 0, 0, 0))
                info.create_system = 3
                info.external_attr = mode << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                package.writestr(info, data)
    payload_archive = args.output / "iphone2g-helper.zip"
    archive(payload_archive, [("openup-iphone2g", output.read_bytes(), 0o100755)])
    manifest["archive_sha256"] = hashlib.sha256(payload_archive.read_bytes()).hexdigest()
    (args.output / "iphone2g-helper.json").write_text(json.dumps(manifest, indent=2) + "\n")
    repository = root.parent.parent
    shared = root.parent / "iphone-dataprotection"
    paths = [repository / "LICENSE"]
    paths += [p for p in root.iterdir() if p.is_file() and p.suffix in (".c", ".py", ".txt", ".json")]
    paths += [p for folder in (shared / "include", shared / "licenses") for p in folder.rglob("*") if p.is_file()]
    paths += [shared / name for name in ("BUILD.txt", "NOTICE.txt", "PROVENANCE.json", "ld64-host-page-size.patch")]
    paths += [shared / "source/ramdisk_tools/IOUSBDeviceControllerLib.h"]
    archive(args.output / "iphone2g-source.zip", [
        (p.relative_to(repository).as_posix(), p.read_bytes(), 0o100644) for p in sorted(paths)])


if __name__ == "__main__":
    main()

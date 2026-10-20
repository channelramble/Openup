"""Patch only the decoded iPhone 3G iOS 4.2.1 / 8C148 kernel.

The patch byte patterns originate from the iPhone Data Protection project:
https://github.com/nabla-c0d3/iphone-dataprotection/blob/
572dd5cd8c07f5f14f7ea9488041031dd22a26bb/python_scripts/kernel_patcher.py
(patchs_armv6, by the iPhone Data Protection project contributors).

Original licensing evidence:
https://storage.googleapis.com/google-code-archive/v2/code.google.com/
iphone-dataprotection/project.json declares "license": "bsd".
https://code.google.com/archive/archive.js maps that identifier to the
"New BSD License": https://opensource.org/licenses/BSD-3-Clause .
The archived source has no project-specific copyright notice to reproduce.
The validation and file-writing implementation below is new application code.

Original BSD-3-Clause terms:
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
"""

import argparse
import hashlib
import os
import struct
import sys
from pathlib import Path

from .core import ResetError, read_local_file, secure_write
from .host import reject_link

# Verified from Apple's original IPSW, fetched over HTTPS and checked against
# its ZIP entry CRC before IMG3 AES-CBC decoding and LZSS decompression.
# https://secure-appldnld.apple.com/iPhone4/061-9853.20101122.Vfgt5/
# iPhone1,2_4.2.1_8C148_Restore.ipsw
# Original kernelcache.release.n82 IMG3 SHA-256:
# 73e7938f118714027c6f85f073aceb8cc545d88e20dc0eb875fc52f17f89199f
# Decoded size: 8,343,552 bytes. No Apple firmware is distributed here.
KERNEL_SHA256 = "f30d1ca0d33b4519c9f956c1fe9d86301a5be746b06c40174f2f2acb2c03b623"
MAX_KERNEL_SIZE = 16 * 1024 * 1024
PATCHES = (
    ("NAND_epoch", bytes.fromhex("00 00 5B E1 0E 00 00 0A"),
     bytes.fromhex("00 00 5B E1 0E 00 00 EA")),
    ("CSED", bytes.fromhex("00 00 00 00 01 00 00 00 80 00 00 00 00 00 00 00"),
     bytes.fromhex("01 00 00 00 01 00 00 00 80 00 00 00 00 00 00 00")),
    ("AMFI", bytes.fromhex("00 00 00 0A 00 40 A0 E3 04 00 A0 E1 90 80 BD E8"),
     bytes.fromhex("00 00 00 0A 00 40 A0 E3 01 00 A0 E3 90 80 BD E8")),
    ("_PE_i_can_has_debugger", bytes.fromhex("00 28 0B D0 07 4A 13 68 00 2B 02 D1 03 60 10 68"),
     bytes.fromhex("01 20 70 47 07 4A 13 68 00 2B 02 D1 03 60 10 68")),
    ("IOAESAccelerator enable UID", bytes.fromhex("5D D0 36 4B 9A 42"),
     bytes.fromhex("00 20 36 4B 9A 42")),
    ("IOAES gid", bytes.fromhex("FA 23 9B 00 9A 42 05 D1"),
     bytes.fromhex("00 20 00 20 9A 42 05 D1")),
    ("nand-disable-driver", b"nand-disable-driver\x00", b"nand-disable" + b"\x00" * 8),
)


def patch_kernel(data):
    """Return a patched copy after checking the complete, known kernel input."""
    if not isinstance(data, (bytes, bytearray)) or not 28 <= len(data) <= MAX_KERNEL_SIZE:
        raise ResetError("Expected a bounded, decoded iPhone 3G kernel.")
    data = bytes(data)
    magic, cpu, subtype, file_type = struct.unpack_from("<IIII", data)
    if (magic, cpu, subtype & 0xFFFFFF, file_type) != (0xFEEDFACE, 12, 6, 2):
        raise ResetError("Expected a decoded ARMv6 Mach-O kernel for iPhone 3G.")
    if hashlib.sha256(data).hexdigest() != KERNEL_SHA256:
        raise ResetError("Kernel is not the original iPhone 3G 4.2.1 / 8C148 image.")

    changes = []
    for name, before, after in PATCHES:
        if not before or len(before) != len(after) or data.count(before) != 1:
            raise ResetError(f"Kernel patch {name!r} does not match exactly once.")
        start = data.index(before)
        changes.append((start, start + len(before), after))
    changes.sort()
    if any(left[1] > right[0] for left, right in zip(changes, changes[1:])):
        raise ResetError("Kernel patch ranges overlap.")
    result = bytearray(data)
    for start, end, replacement in changes:
        result[start:end] = replacement
    return bytes(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if (args.input.resolve() == args.output.resolve()
                or (args.output.exists() and os.path.samefile(args.input, args.output))):
            raise ResetError("Kernel output must be separate from the original input.")
        reject_link(args.output)
        patched = patch_kernel(read_local_file(args.input, MAX_KERNEL_SIZE))
        secure_write(args.output, patched)
        print("Validated and patched iPhone 3G 4.2.1 / 8C148 kernel (7 patches).")
        return 0
    except (ResetError, OSError) as exc:
        print(f"Kernel patch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

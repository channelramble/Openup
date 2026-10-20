"""Validation helpers used by the externally downloaded ramdisk backend."""

import argparse
import re
import struct
import sys
from pathlib import Path

from .core import ResetError, read_local_file

# Product -> board, chip ID, board ID. Source: libirecovery's device table.
PHONES = {
    "iPhone1,1": ("m68", 0x8900, 0x00),
    "iPhone1,2": ("n82", 0x8900, 0x04),
    "iPhone2,1": ("n88", 0x8920, 0x00),
    "iPhone3,1": ("n90", 0x8930, 0x00),
    "iPhone3,2": ("n90b", 0x8930, 0x04),
    "iPhone3,3": ("n92", 0x8930, 0x06),
    "iPhone4,1": ("n94", 0x8940, 0x08),
    "iPhone5,1": ("n41", 0x8950, 0x00),
    "iPhone5,2": ("n42", 0x8950, 0x02),
    "iPhone5,3": ("n48", 0x8950, 0x0A),
    "iPhone5,4": ("n49", 0x8950, 0x0E),
}


def resolve_identity(product, board, expected="", recovery=""):
    fields = dict(re.findall(r"^([A-Z]+):\s*(.*?)\s*$", recovery, re.MULTILINE))
    if "CPID" in fields and "BDID" in fields:
        try:
            chip, board_id = (int(fields[key], 16) for key in ("CPID", "BDID"))
        except ValueError as exc:
            raise ResetError("Malformed recovery chip/board ID.") from exc
        matches = [key for key, (_, cp, bd) in PHONES.items() if (cp, bd) == (chip, board_id)]
        if len(matches) != 1:
            raise ResetError("Unrecognized recovery chip/board combination.")
        # Raw chip/board IDs resolve S5L8900 DFU product-name ambiguity.
        product = matches[0]
    elif product not in PHONES:
        raise ResetError("Cannot identify this legacy iPhone. Re-enter DFU and reconnect.")
    elif board and board.lower().removesuffix("ap") != PHONES[product][0]:
        raise ResetError("Product and board disagree; refusing to choose a boot image.")
    if expected and product != expected:
        raise ResetError(f"Connected device is {product}, but --model selects {expected}.")
    return product, PHONES[product][0]


def macho_architectures(data):
    """Return ARM subtypes from thin or fat Mach-O headers, without executing."""
    if len(data) < 28:
        raise ResetError("Truncated on-device executable.")
    magic = data[:4]
    if magic in (b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"):
        endian = "<" if magic[0] == 0xCE else ">"
        cpu, subtype, file_type = struct.unpack_from(endian + "III", data, 4)
        if cpu != 12 or file_type != 2:
            return set()
        return {subtype & 0xFFFFFF}
    if magic == b"\xca\xfe\xba\xbe":
        count = struct.unpack_from(">I", data, 4)[0]
        if not 1 <= count <= 16 or len(data) < 8 + count * 20:
            raise ResetError("Malformed universal Mach-O header.")
        result = set()
        for index in range(count):
            cpu, subtype, offset, size, _ = struct.unpack_from(">IIIII", data, 8 + index * 20)
            if offset + size > len(data) or offset < 8 + count * 20:
                raise ResetError("Invalid Mach-O slice bounds.")
            if cpu == 12:
                result.update(macho_architectures(data[offset:offset + size]))
        return result
    raise ResetError("Expected an iOS Mach-O executable.")


def check_payloads(model, folder):
    if model not in PHONES:
        raise ResetError("Unsupported payload target.")
    armv6 = PHONES[model][1] == 0x8900
    for name in ("bruteforce", "device_infos", "restored_external"):
        architectures = macho_architectures(read_local_file(Path(folder) / name, 16 * 1024 * 1024))
        if not architectures or (armv6 and not architectures.intersection({0, 5, 6})):
            raise ResetError(
                f"{name} is incompatible with {model}. The upstream bundle contains ARMv7 "
                "executables; iPhone 3G needs ARMv6 builds. Supply --armv6-tools with "
                "compatible, ramdisk-tested executables, or use prepare for timer reset."
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identity = commands.add_parser("identity")
    for field in ("product", "board", "expected", "recovery"):
        identity.add_argument(field)
    payloads = commands.add_parser("payloads")
    payloads.add_argument("model")
    payloads.add_argument("folder")
    args = parser.parse_args()
    try:
        if args.command == "identity":
            print("\t".join(resolve_identity(args.product, args.board, args.expected, args.recovery)))
        else:
            check_payloads(args.model, args.folder)
        return 0
    except (ResetError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

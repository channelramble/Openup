"""Reviewed iPhone 3G timer-ramdisk compatibility edits for Legacy iOS Kit."""

import argparse
import hashlib
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .backend_support import resolve_identity
from .core import ResetError

SCRIPT_SHA256 = "cfec0415c3eb312a10136a050e2b5a83c893877741684f603bf0c3458862a17f"


def select_3g_identity(stage, mode, product, board, recovery):
    """WTF's generic m68 identity is not a physical board identification."""
    fields = dict(re.findall(r"^([A-Z]+):\s*(.*?)\s*$", recovery, re.MULTILINE))
    if mode == "Normal" and stage == "prepare":
        return resolve_identity(product, board, "iPhone1,2")
    if fields.get("MODE") != mode or mode not in ("WTF", "DFU", "Recovery"):
        raise ResetError("Recovery mode changed or could not be read; reconnect and retry.")
    try:
        chip = int(fields["CPID"], 16)
    except (KeyError, ValueError) as exc:
        raise ResetError("Missing or malformed recovery chip ID.") from exc
    if mode == "WTF" and stage == "prepare":
        if chip != 0x8900:
            raise ResetError("Connected WTF device is not an S5L8900 iPhone 3G target.")
        # This chooses download inputs only. The real DFU board is checked before
        # any board-specific firmware upload, after the shared WTF transition.
        return "iPhone1,2", "n82"
    if "BDID" not in fields:
        raise ResetError("Missing recovery board ID; cannot verify an iPhone 3G.")
    identity = resolve_identity(product, board, "iPhone1,2", recovery)
    if mode == "DFU" and fields.get("SRTG") != "iBoot-636.66.3x":
        raise ResetError("Expected patched S5L8900 DFU was not detected. Re-enter WTF mode and retry.")
    if stage == "upload" and mode != "DFU":
        raise ResetError("Verified patched DFU is required before uploading the 3G iBSS.")
    return identity


HOOK = r'''
bl_ios_legacy_identity() {
    local recovery resolved product="$device_type" board="$device_model"
    if [[ $device_mode == "Normal" && $1 == "prepare" ]]; then
        product="$($ideviceinfo -s -k ProductType)"
        board="$($ideviceinfo -s -k HardwareModel)"
    else
        recovery="$($irecovery -q 2>/dev/null)" ||
            error "Could not read device identity; no board-specific firmware was uploaded."
    fi
    resolved="$("$BL_IOS_PYTHON" -m ios_timeout.legacy_kit identity \
        "$1" "$device_mode" "$product" "$board" "$recovery")" ||
        error "iPhone 3G identity verification failed; firmware upload cancelled."
    IFS=$'\t' read -r device_type device_model <<< "$resolved"
    if [[ $device_mode == "WTF" ]]; then
        log "WTF identity is generic. Preparing selected iPhone1,2 / n82ap; real DFU identity will be checked before iBSS."
    else
        log "Verified iPhone 3G target: $device_type / ${device_model}ap ($device_mode)."
    fi
}

bl_ios_legacy_diagnostics() {
    local recovery
    recovery="$($irecovery -q 2>/dev/null)"
    if [[ -z $recovery ]]; then
        warn "USB diagnostic: irecovery cannot currently read the phone."
    else
        print "* Last USB state (read only):"
        printf '%s\n' "$recovery" | grep -E '^(MODE|CPID|BDID|PRODUCT|MODEL|SRTG):'
    fi
}
'''


def adapt_script(original):
    if hashlib.sha256(original).hexdigest() != SCRIPT_SHA256:
        raise ResetError(
            "This Legacy iOS Kit script is not the reviewed version for the 3G identity fix. "
            "Use the app's pinned backend, or supply an unchanged checkout of its pinned revision."
        )
    source = original.decode("utf-8")

    def replace(before, after):
        nonlocal source
        if source.count(before) != 1:
            raise ResetError("Legacy iOS Kit compatibility edit did not match exactly.")
        source = source.replace(before, after)

    replace("device_entry() {\n", HOOK + "\ndevice_entry() {\n")
    replace(
        '    if [[ $no_version_check == 1 && ! -e ../resources/new ]]; then\n',
        '    if [[ $no_version_check == 1 ]]; then\n',
    )
    # --device sets entry mode, suppressing the usual ambiguous-WTF prompt.
    # The upstream board fallback must never replace the chosen 3G with m68.
    replace(
        '            device_model=$($irecovery -q | grep "MODEL" | cut -c 8-)\n',
        '            device_model=$($irecovery -q | grep "MODEL" | cut -c 8-)\n'
        '            bl_ios_legacy_identity prepare\n',
    )
    replace(
        '    device_model="$(echo $device_model | tr \'[:upper:]\' \'[:lower:]\')"\n',
        '    if [[ $device_mode == "Normal" ]]; then bl_ios_legacy_identity prepare; fi\n'
        '    device_model="$(echo $device_model | tr \'[:upper:]\' \'[:lower:]\')"\n',
    )
    replace(
        '        device_pwnd="Pwnage 2.0"\n    fi\n}\n\ndevice_get_info()',
        '        device_pwnd="Pwnage 2.0"\n    fi\n'
        '    bl_ios_legacy_identity upload\n}\n\ndevice_get_info()',
    )
    replace(
        '    $irecovery -f "$wtf_patched"\n',
        '    $irecovery -f "$wtf_patched" || error "Shared WTF upload failed; boot cancelled."\n',
    )
    # Restrict edits to the actual timer-ramdisk function; unrelated restore
    # commands remain untouched, and the original checkout is never rewritten.
    start = source.index("device_ramdisk() {\n")
    end = source.index("\ndevice_", start + 1)
    section = source[start:end]
    before = '    if [[ $device_type == "iPad1,1" && $build_id != "9"* ]]; then\n'
    if section.count(before) != 1:
        raise ResetError("Legacy ramdisk upload boundary changed.")
    section = section.replace(before, '    bl_ios_legacy_identity upload\n\n' + before)
    if section.count("    device_find_mode Recovery\n") != 1:
        raise ResetError("Legacy ramdisk recovery wait changed.")
    section = section.replace(
        "    device_find_mode Recovery\n",
        '    if [[ $platform == "linux" ]]; then\n'
        "        device_find_mode Recovery 24\n"
        "    else\n"
        "        device_find_mode Recovery 15\n"
        "    fi\n",
    )
    section = re.sub(
        r"^([ \t]+\$irecovery -[fc] .+?)([ \t]+#.*)?$",
        lambda match: match[1] + ' || error "Ramdisk USB command failed; boot cancelled."'
        + (match[2] or ""),
        section, flags=re.MULTILINE,
    )
    source = source[:start] + section + source[end:]
    replace(
        '            * ) error "Failed to find device in $mode mode (Timed out). Please run the script again.";;\n',
        '            * ) bl_ios_legacy_diagnostics\n'
        '                error "Failed to find device in $mode mode (Timed out). No timer reset was attempted.";;\n',
    )
    return source.encode("utf-8")


@contextmanager
def prepared_script(kit, original):
    """Keep dirname/$0-based upstream resources correct without modifying it."""
    adapted = adapt_script(original)
    path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".bl-ios-prepare-", suffix=".sh", dir=kit,
                                         delete=False) as stream:
            path = Path(stream.name)
            stream.write(adapted)
        yield path
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["identity"])
    parser.add_argument("stage", choices=["prepare", "upload"])
    for name in ("mode", "product", "board", "recovery"):
        parser.add_argument(name)
    args = parser.parse_args()
    try:
        print("\t".join(select_3g_identity(
            args.stage, args.mode, args.product, args.board, args.recovery)))
    except ResetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

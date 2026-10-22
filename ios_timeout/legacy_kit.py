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


def script_is_pinned(original):
    """True when these bytes are the reviewed, pinned Legacy iOS Kit restore.sh."""
    return hashlib.sha256(original).hexdigest() == SCRIPT_SHA256


def _require_pinned(original):
    if not script_is_pinned(original):
        raise ResetError(
            "This Legacy iOS Kit script is not the reviewed version for the 3G identity fix. "
            "Use the app's pinned backend, or supply an unchanged checkout of its pinned revision."
        )


def _disable_sshrd_menu(source):
    """Return from the SSH Ramdisk Menu immediately when Openup drives the kit.

    Openup only needs the ramdisk booted; its own reset resumes once the backend
    exits. The interactive menu would otherwise force a terminal "Select an
    option" / "Choose the next step" choice. With ``BL_IOS_SSHRD_AUTOEXIT=1`` the
    menu returns as if Exit were chosen (leaving the phone in the ramdisk), so it
    is never entered and no device operation is auto-answered in a loop. The
    stock interactive menu is unchanged when the flag is unset. This edit is
    model-independent, so it applies to every supported timer-reset model.
    """
    before = ('    menu_items+=("Reboot Device" "Exit")\n\n'
              '    print "* For accessing data, note the following:"\n')
    after = ('    menu_items+=("Reboot Device" "Exit")\n\n'
             '    if [[ ${BL_IOS_SSHRD_AUTOEXIT:-} == 1 ]]; then\n'
             '        log "SSH ramdisk is ready. Openup finishes the timer reset automatically; '
             'no menu choice is needed."\n'
             '        return 0\n'
             '    fi\n\n'
             '    print "* For accessing data, note the following:"\n')
    if source.count(before) != 1:
        raise ResetError("Legacy iOS Kit SSH ramdisk menu layout changed.")
    return source.replace(before, after)


def _release_kit_agent_control(source):
    """Make Openup's device-agent guardian the sole owner of Apple's AMP agents.

    The kit STOPs AMPDevicesAgent/AMPDeviceDiscoveryAgent/MobileDeviceUpdater at
    startup and only resumes them on exit when --no-finder is unset. Openup runs
    the kit with --no-finder and holds the precise `recovery_agents` guardian
    across the whole operation, so neutralise the kit's own global STOP/CONT:
    otherwise an agent the kit stops that the guardian has not claimed (a narrow
    startup race) is left suspended. Mirrors the recovery backend adapter. The
    command is replaced with a no-op (not deleted) because the kit's resume sits
    inside a conditional whose body must stay non-empty for valid bash.
    """
    adapted = re.sub(r"killall -(?:STOP|CONT) AMP[^\n]*", ":", source)
    if adapted.count("killall -STOP AMP") or adapted.count("killall -CONT AMP"):
        raise ResetError("Legacy iOS Kit agent-control lines changed.")
    return adapted


def adapt_menu_only(original):
    """GUI prompt hooks and a non-interactive SSH ramdisk menu, with no 3G hooks.

    Used for the non-3G timer-reset models so their whole workflow stays in the
    GUI. The 3G identity verification in :func:`adapt_script` is intentionally
    excluded; only the shared prompt/menu handling is applied.
    """
    _require_pinned(original)
    source = _disable_sshrd_menu(original.decode("utf-8"))
    source = _release_kit_agent_control(source)
    from .backend_ui import adapt_prompts
    return adapt_prompts(source, backend="legacy").encode("utf-8")


def adapt_script(original):
    _require_pinned(original)
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
    source = _disable_sshrd_menu(source)
    source = _release_kit_agent_control(source)
    from .backend_ui import adapt_prompts
    return adapt_prompts(source, backend="legacy").encode("utf-8")


@contextmanager
def _prepared(kit, adapted):
    """Run an adapted copy in place so dirname/$0 resources resolve; never edit."""
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


@contextmanager
def prepared_script(kit, original):
    """iPhone 3G preparation: 3G identity checks plus shared prompt/menu handling."""
    with _prepared(kit, adapt_script(original)) as path:
        yield path


@contextmanager
def prepared_menu_script(kit, original):
    """Non-3G preparation: shared GUI prompt/menu handling, no 3G identity hooks."""
    with _prepared(kit, adapt_menu_only(original)) as path:
        yield path


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

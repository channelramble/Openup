"""Shared normal/Recovery/DFU hooks; callers first verify the backend hash."""

from .core import ResetError


HOOK = r'''
bl_ios_entry() {
    local action="$1" result status
    local bl_ios_query=("$irecovery")
    if [[ -n $sudo && $irecovery == "$sudo "* ]]; then
        bl_ios_query=("$sudo" "${irecovery#"$sudo "}")
    elif [[ $irecovery == "sudo "* ]]; then
        bl_ios_query=(sudo "${irecovery#sudo }")
    fi
    result="$("$BL_IOS_PYTHON" -m ios_timeout.backend_entry "$action" \
        --info "$ideviceinfo" --enter "$dir/ideviceenterrecovery" \
        --model "${BL_IOS_MODEL:-${device_type:-}}" \
        --udid "${BL_IOS_UDID:-${device_udid:-}}" \
        --ecid "${bl_ios_entry_ecid:-${device_ecid:-}}" --mode "${device_mode:-}" --query "${bl_ios_query[@]}")"
    status=$?
    [[ $action == detect && $status == 1 ]] && return 1
    [[ $status == 0 ]] || error "Device preparation stopped before firmware upload. Follow the message above and retry."
    IFS=$'\t' read -r device_mode device_type device_model device_ecid device_vers device_udid <<< "$result"
    # Stock metadata reads must never replace the identity established before
    # the normal-to-Recovery transition or its manual Recovery fallback.
    [[ -n $bl_ios_entry_ecid ]] || bl_ios_entry_ecid="$device_ecid"
    [[ $device_udid == - ]] && device_udid=""
    device_vers_maj="${device_vers%%.*}"
    log "Verified device entry: $device_type / ${device_model}ap ($device_mode)."
    return 0
}

bl_ios_verify_entry() {
    # S5L8900 uses separate WTF/patched-DFU identity and stage checks.
    [[ $device_proc == 1 || $device_type == "iPhone1,1" || $device_type == "iPhone1,2" ]] && return 0
    case "$device_mode" in
        DFU | Recovery | iBSS | iBEC ) bl_ios_entry verify;;
        Normal ) :;;
        * ) error "Device entry mode is not verified; no firmware was uploaded.";;
    esac
}
'''


def adapt_entry(source, backend):
    def replace(before, after):
        nonlocal source
        if source.count(before) != 1:
            raise ResetError("The reviewed normal-mode entry layout changed.")
        source = source.replace(before, after)

    replace("device_get_info() {\n", HOOK + "\ndevice_get_info() {\n")
    if backend == "recover":
        replace('    $ideviceinfo -s >/dev/null 2>&1 && device_mode="Normal"\n',
                '    bl_ios_entry detect || :\n')
        before = '''            [[ -n $device_argmode ]] && device_entry || {
                device_type=$($ideviceinfo -s -k ProductType 2>/dev/null)
                [[ -z $device_type ]] && device_type=$($ideviceinfo -k ProductType 2>/dev/null)
                device_ecid=$($ideviceinfo -s -k UniqueChipID 2>/dev/null)
                device_model=$($ideviceinfo -s -k HardwareModel 2>/dev/null)
                device_vers=$($ideviceinfo -s -k ProductVersion 2>/dev/null)
                device_udid=$($ideviceinfo -s -k UniqueDeviceID 2>/dev/null)
                [[ -z $device_udid ]] && device_udid=$($ideviceinfo -k UniqueDeviceID 2>/dev/null)
            }'''
        replace(before, '            : # Normal identity was read from the selected USB serial above.')
        replace('''                log "Entering recovery mode..."
                "$dir/ideviceenterrecovery" "$device_udid" >/dev/null
                device_find_mode Recovery 50''', '                bl_ios_entry recovery')
        replace('''    esac
    all_flash="Firmware/all_flash/all_flash.${device_model}ap.production"''',
                '''    esac
    bl_ios_verify_entry
    all_flash="Firmware/all_flash/all_flash.${device_model}ap.production"''')
    elif backend == "legacy":
        before = '''        $ideviceinfo -s >/dev/null
        if [[ $? == 0 ]]; then
            device_mode="Normal"
        else
            $ideviceinfo >/dev/null
            if [[ $? == 0 ]]; then
                device_mode="Normal"
            fi
        fi'''
        replace(before, '        bl_ios_entry detect || :')
        # --sshrd intentionally skips the stock normal metadata block. Populate
        # that identity above and keep its UDID/version available for entry.
        start = source.index('        "Normal" )\n', source.index("device_get_info() {\n"))
        end = source.index('\n        ;;', start)
        source = source[:start] + '''        "Normal" )
            : # Selected normal identity is already verified; do not enter manual mode.''' + source[end:]
        start = source.index('        "Recovery" )\n', source.index("device_enter_mode() {\n"))
        end = source.index('\n        ;;', start)
        source = source[:start] + '''        "Recovery" )
            if [[ $device_mode == "Normal" ]]; then
                bl_ios_entry recovery
            fi''' + source[end:]
        replace('\n    device_fw_dir="../saved/firmware/$device_type"\n',
                '\n    bl_ios_verify_entry\n    device_fw_dir="../saved/firmware/$device_type"\n')
    else:
        raise ValueError("Unknown entry backend.")
    # A locked normal phone can already have completed the managed manual
    # Recovery path. Re-reading stock metadata here can mix endpoints and
    # starts an unnecessary blocking Recovery console session in Legacy Kit.
    start = source.index('    case $device_mode in\n', source.index("device_get_info() {\n"))
    body = source.index('\n', source.index(' )\n', start)) + 1
    end = source.index('\n        ;;', body)
    source = source[:body] + '            if [[ -z $bl_ios_entry_ecid && -z $bl_ios_2g_identity_ready ]]; then\n' + source[body:end] + \
        '\n            fi' + source[end:]
    replace("device_dfuhelper() {\n", '''device_dfuhelper() {
    if [[ $device_proc != 1 ]]; then
        if [[ $device_mode == DFU ]]; then
            bl_ios_verify_entry
        else
            bl_ios_entry dfu
        fi
        return
    fi
''')
    return source
